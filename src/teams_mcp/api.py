from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from html import escape
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .auth import AuthManager
from .config import Settings
from .errors import ApiError
from .models import CredentialBundle

Route = Literal["chat", "csa", "substrate", "transcript", "public"]
_CHAT_HOST = re.compile(r"^(amer|emea|apac)\.ng\.msg\.teams\.microsoft\.com$")
_TRANSCRIPT_HOST_SUFFIXES = (
    ".asm.skype.com",
    ".asyncgw.teams.microsoft.com",
    ".teams.microsoft.com",
)
_UNAUTHENTICATED_DOWNLOAD_SUFFIXES = (
    ".blob.core.windows.net",
    ".azureedge.net",
    ".azurefd.net",
)


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix.removeprefix(".") or host.endswith(suffix) for suffix in suffixes)


def _request_id(response: httpx.Response) -> str | None:
    for name in ("x-ms-request-id", "request-id", "x-request-id", "client-request-id"):
        value = response.headers.get(name)
        if value:
            return str(value)[:200]
    return None


class TeamsApi:
    def __init__(
        self,
        auth: AuthManager,
        settings: Settings | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.auth = auth
        self.settings = settings or auth.settings
        self._http = http
        self._owned_http: httpx.AsyncClient | None = None
        self._snapshot_cache: tuple[float, dict[str, Any]] | None = None

    async def __aenter__(self) -> TeamsApi:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_http is not None:
            await self._owned_http.aclose()
            self._owned_http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        if self._owned_http is None:
            self._owned_http = httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=False,
                headers={"User-Agent": "teams-playwright-mcp-server/0.2"},
            )
        return self._owned_http

    @staticmethod
    def _chat_base(bundle: CredentialBundle) -> str:
        if bundle.region not in {"amer", "emea", "apac"}:
            raise ApiError("The stored Teams region is invalid.", "region_invalid")
        return f"https://{bundle.region}.ng.msg.teams.microsoft.com/v1"

    @staticmethod
    def _validate_url(url: str, route: Route, bundle: CredentialBundle | None = None) -> None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ApiError("Refusing a non-HTTPS or credential-bearing URL.", "url_rejected")
        if route == "chat":
            if not _CHAT_HOST.fullmatch(host):
                raise ApiError(
                    "Refusing to send a Teams token to an unknown chat host.", "host_rejected"
                )
            if bundle is not None and host != urlsplit(TeamsApi._chat_base(bundle)).hostname:
                raise ApiError(
                    "The chat URL does not match the authenticated region.", "region_mismatch"
                )
        elif route == "csa":
            if host != "teams.microsoft.com":
                raise ApiError(
                    "Refusing to send a Teams token to an unknown CSA host.", "host_rejected"
                )
        elif route == "substrate":
            if host != "substrate.office.com":
                raise ApiError(
                    "Refusing to send a Substrate token to an unknown host.",
                    "host_rejected",
                )
        elif route == "transcript":
            if host != "teams.microsoft.com" and not _host_matches(host, _TRANSCRIPT_HOST_SUFFIXES):
                raise ApiError(
                    "Refusing to send a Teams token to an unknown transcript host.", "host_rejected"
                )
        elif route == "public":
            if not _host_matches(host, _UNAUTHENTICATED_DOWNLOAD_SUFFIXES):
                raise ApiError("Refusing an unknown transcript redirect host.", "host_rejected")

    @staticmethod
    def _headers(
        route: Route,
        bundle: CredentialBundle | None,
        *,
        resource_token: str | None = None,
    ) -> dict[str, str]:
        if route == "public":
            return {}
        if bundle is None:
            raise ApiError("Authenticated route called without credentials.", "auth_internal")
        if route == "chat":
            return {"Authentication": f"skypetoken={bundle.skype_token}"}
        if route in {"csa", "substrate"}:
            if resource_token is None:
                raise ApiError("Resource route called without its bearer token.", "auth_internal")
            return {"Authorization": f"Bearer {resource_token}"}
        return {"Authorization": f"skype_token {bundle.skype_token}"}

    async def _request(
        self,
        method: str,
        url: str,
        route: Route,
        *,
        json_body: object | None = None,
        retry_auth: bool = True,
    ) -> httpx.Response:
        bundle = None if route == "public" else await self.auth.get_bundle()
        resource_token: str | None = None
        if route == "csa":
            resource_token = await self.auth.get_csa_token()
        elif route == "substrate":
            resource_token = await self.auth.get_substrate_token()
        self._validate_url(url, route, bundle)
        headers = self._headers(route, bundle, resource_token=resource_token)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(4):
            try:
                response = await self._client().request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                )
            except httpx.HTTPError as exc:
                if attempt < 3:
                    await asyncio.sleep(0.2 * 2**attempt)
                    continue
                raise ApiError(
                    "Teams request failed at the network layer.", "network_error"
                ) from exc

            if len(response.content) > self.settings.max_response_bytes:
                raise ApiError(
                    "Teams response exceeded the configured size limit.", "response_too_large"
                )
            if response.status_code == 401 and retry_auth and bundle is not None:
                if route == "csa":
                    resource_token = await self.auth.get_csa_token(force=True)
                elif route == "substrate":
                    resource_token = await self.auth.get_substrate_token(force=True)
                else:
                    bundle = await self.auth.refresh(bundle, force=True)
                headers = self._headers(route, bundle, resource_token=resource_token)
                if json_body is not None:
                    headers["Content-Type"] = "application/json"
                retry_auth = False
                continue
            if response.status_code == 429 and attempt < 3:
                retry_after = response.headers.get("Retry-After", "1")
                try:
                    delay = min(max(float(retry_after), 0.1), 30.0)
                except ValueError:
                    delay = 1.0
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 500 and attempt < 3:
                await asyncio.sleep(0.2 * 2**attempt)
                continue
            if response.is_redirect and route in {"transcript", "public"}:
                return response
            if not response.is_success:
                raise ApiError(
                    f"Teams returned HTTP {response.status_code}.",
                    f"http_{response.status_code}",
                    status_code=response.status_code,
                    request_id=_request_id(response),
                )
            return response
        raise ApiError("Teams request failed after retries.", "max_retries")

    async def _json(
        self,
        method: str,
        url: str,
        route: Route,
        *,
        json_body: object | None = None,
    ) -> object:
        response = await self._request(method, url, route, json_body=json_body)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ApiError("Teams returned malformed JSON.", "invalid_json") from exc

    async def user_properties(self) -> dict[str, Any]:
        bundle = await self.auth.get_bundle()
        value = await self._json("GET", f"{self._chat_base(bundle)}/users/ME/properties", "chat")
        return value if isinstance(value, dict) else {}

    async def list_conversations(self, page_size: int = 500) -> list[dict[str, Any]]:
        bundle = await self.auth.get_bundle()
        size = min(max(page_size, 1), 500)
        url = (
            f"{self._chat_base(bundle)}/users/ME/conversations"
            f"?view=msnp24Equivalent&pageSize={size}"
        )
        value = await self._json("GET", url, "chat")
        if not isinstance(value, dict) or not isinstance(value.get("conversations"), list):
            return []
        return [item for item in value["conversations"] if isinstance(item, dict)]

    async def message_pages(
        self,
        conversation_id: str,
        *,
        page_size: int = 100,
        max_pages: int = 1,
        before_ms: int | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        bundle = await self.auth.get_bundle()
        encoded = quote(conversation_id, safe="")
        size = min(max(page_size, 1), 200)
        if before_ms is None:
            url = (
                f"{self._chat_base(bundle)}/users/ME/conversations/"
                f"{encoded}/messages?pageSize={size}"
            )
        else:
            url = (
                f"{self._chat_base(bundle)}/users/ME/conversations/{encoded}/messages"
                f"?startTime=1&endTime={max(before_ms, 1)}&pageSize={size}"
            )
        seen: set[str] = set()
        for _ in range(min(max(max_pages, 1), 20)):
            if url in seen:
                break
            seen.add(url)
            value = await self._json("GET", url, "chat")
            if not isinstance(value, dict):
                break
            messages = value.get("messages")
            yield (
                [item for item in messages if isinstance(item, dict)]
                if isinstance(messages, list)
                else []
            )
            metadata = value.get("_metadata")
            backward = metadata.get("backwardLink") if isinstance(metadata, dict) else None
            if not isinstance(backward, str) or not backward:
                break
            url = urljoin(self._chat_base(bundle), backward)
            self._validate_url(url, "chat", bundle)

    async def teams_snapshot(self, *, force: bool = False) -> dict[str, Any]:
        cached = self._snapshot_cache
        if not force and cached is not None and cached[0] > time.monotonic():
            return cached[1]
        value = await self._json(
            "GET",
            (
                "https://teams.microsoft.com/api/csa/api/v1/teams/users/me"
                "?isPrefetch=false&enableMembershipSummary=true"
            ),
            "csa",
        )
        if not isinstance(value, dict):
            raise ApiError("Teams CSA returned an invalid snapshot.", "snapshot_invalid")
        self._snapshot_cache = (time.monotonic() + 60, value)
        return value

    async def channel_messages(self, team_id: str, channel_id: str, *, limit: int = 50) -> object:
        del team_id
        messages: list[dict[str, Any]] = []
        async for page in self.message_pages(
            channel_id,
            page_size=min(max(limit, 1), 200),
            max_pages=3,
        ):
            messages.extend(page)
            if len(messages) >= limit:
                break
        return messages[:limit]

    async def channel_replies(
        self,
        team_id: str,
        channel_id: str,
        root_message_id: str,
        *,
        limit: int = 50,
    ) -> object:
        del team_id
        conversation_id = f"{channel_id};messageid={root_message_id}"
        messages: list[dict[str, Any]] = []
        async for page in self.message_pages(
            conversation_id,
            page_size=min(max(limit + 1, 1), 200),
            max_pages=3,
        ):
            messages.extend(page)
            if len(messages) >= limit + 1:
                break
        return messages[: limit + 1]

    async def post_chat_message(self, conversation_id: str, content: str) -> object:
        bundle = await self.auth.get_bundle()
        encoded = quote(conversation_id, safe="")
        now_ms = str(int(time.time() * 1000))
        return await self._json(
            "POST",
            f"{self._chat_base(bundle)}/users/ME/conversations/{encoded}/messages",
            "chat",
            json_body={
                "content": escape(content),
                "messagetype": "RichText/Html",
                "contenttype": "text",
                "clientmessageid": now_ms,
                "imdisplayname": bundle.display_name,
                "properties": {"importance": "", "subject": None},
            },
        )

    async def post_channel_message(
        self,
        team_id: str,
        channel_id: str,
        content: str,
        *,
        root_message_id: str | None = None,
    ) -> object:
        del team_id
        conversation_id = (
            f"{channel_id};messageid={root_message_id}" if root_message_id else channel_id
        )
        return await self.post_chat_message(conversation_id, content)

    async def transcript_vtt(self, url: str) -> str:
        response = await self._request("GET", url, "transcript")
        redirects = 0
        while response.is_redirect and redirects < 2:
            location = response.headers.get("Location")
            if not location:
                raise ApiError("Transcript redirect had no destination.", "redirect_invalid")
            url = urljoin(url, location)
            host = (urlsplit(url).hostname or "").lower()
            route: Route = (
                "transcript" if _host_matches(host, _TRANSCRIPT_HOST_SUFFIXES) else "public"
            )
            self._validate_url(url, route)
            response = await self._request("GET", url, route)
            redirects += 1
        if response.is_redirect:
            raise ApiError("Too many transcript redirects.", "redirect_limit")
        return response.text
