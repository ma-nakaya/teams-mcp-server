"""Experimental, self-only legacy AMS file transport; no new OAuth scopes.

Wire references (protocol facts, not copied implementations):
https://github.com/Terrance/SkPy/blob/master/skpy/chat.py (sendFile)
https://github.com/EionRobb/purple-teams/blob/master/teams_messages.c (AMS image upload)
https://github.com/EionRobb/purple-teams/blob/master/libteams.h (Teams client headers)
Generic files in current Teams remain experimental. Privacy evidence is the
accepted explicit self-only CREATE request, not an unsupported server ACL readback.
The service layer must apply write guards, audit, durable receipts and readback checks.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any
from uuid import UUID

import httpx

from .api import TeamsApi
from .auth import _jwt_claims
from .errors import ApiError
from .models import CredentialBundle

_AMS_BASE = "https://as-prod.asyncgw.teams.microsoft.com/v1/objects"
# AMS derives its client platform from User-Agent and rejects generic HTTP clients.
# Keep these protocol compatibility values fixed, using purple-teams' documented
# Teams desktop signature rather than any user/browser fingerprint or credential.
_AMS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "Edg/126.0.0.0 Teams/24165.1410.2974.6689/49"
)
_AMS_CLIENT_VERSION = "49/25113001312"
_SELF_MESSAGES = "/users/ME/conversations/48%3Anotes/messages"
_OBJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,511}\Z")
_MESSAGE_ID = re.compile(r"[0-9]{1,32}\Z")
_MRI = re.compile(
    r"(?:8:)?orgid:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\Z"
)


def _reject(message: str, code: str) -> ApiError:
    return ApiError(message, code)


def _object_id(value: str) -> str:
    if not isinstance(value, str) or not _OBJECT_ID.fullmatch(value):
        raise _reject("Invalid file object identifier.", "file_object_invalid")
    return value


def _filename(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value in {".", ".."}
        or len(value.encode("utf-8", errors="replace")) > 255
        or any(char in value for char in ("/", "\\", ":"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise _reject("Invalid attachment filename.", "file_name_invalid")
    return value


def _client_id(value: str) -> str:
    if not isinstance(value, str) or not _MESSAGE_ID.fullmatch(value):
        raise _reject("Invalid attachment client identifier.", "file_client_id_invalid")
    return value


def _normalize_mri(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _MRI.fullmatch(value)
    return f"8:orgid:{match.group(1).lower()}" if match else None


def _self_mri(bundle: CredentialBundle) -> str:
    # Metadata comes only from the existing credential manager's trusted token.
    # No caller can choose an identity, and the token never leaves an HTTP header.
    try:
        identity = _normalize_mri(_jwt_claims(bundle.skype_token).get("skypeid"))
    except (ValueError, UnicodeError):
        raise _reject(
            "Authenticated self identity is unavailable.", "file_identity_invalid"
        ) from None
    if identity is None:
        raise _reject("Authenticated self identity is unavailable.", "file_identity_invalid")
    return identity


def _tenant_id(value: object) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise _reject(
            "Authenticated tenant identity is unavailable.", "file_identity_invalid"
        ) from None


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except (ValueError, UnicodeDecodeError):
        raise _reject("Invalid file service response.", "file_response_invalid") from None
    if not isinstance(value, dict):
        raise _reject("Invalid file service response.", "file_response_invalid")
    return value


class TeamsFileApi:
    """A fixed-host adapter that can post attachments only to the authenticated self."""

    def __init__(self, api: TeamsApi) -> None:
        self.api = api
        self._expected_identity: tuple[str, str] | None = None
        self._created: dict[str, tuple[str, str, str]] = {}

    def bind_identity(self, tenant_id: str, principal: str) -> None:
        """Pin the orchestrator's authenticated identity snapshot for this instance."""
        me = _normalize_mri(principal)
        if me is None:
            raise _reject("Authenticated self identity is unavailable.", "file_identity_invalid")
        identity = (_tenant_id(tenant_id), me)
        if self._expected_identity is not None and self._expected_identity != identity:
            raise _reject("Authenticated file identity changed.", "file_identity_changed")
        self._expected_identity = identity

    def _identity(self, bundle: CredentialBundle) -> tuple[str, str]:
        identity = (_tenant_id(bundle.tenant_id), _self_mri(bundle))
        self.bind_identity(*identity)
        return identity

    def _remember_created(self, identifier: str, name: str, bundle: CredentialBundle) -> None:
        tenant, principal = self._identity(bundle)
        provenance = (tenant, principal, name)
        if identifier in self._created and self._created[identifier] != provenance:
            raise _reject(
                "File creation record conflicts with this request.", "file_provenance_unverified"
            )
        self._created[identifier] = provenance

    def _created_file(
        self, identifier: str, bundle: CredentialBundle, filename: str | None = None
    ) -> tuple[str, str, str]:
        identity = self._identity(bundle)
        provenance = self._created.get(identifier)
        if (
            provenance is None
            or provenance[:2] != identity
            or (filename is not None and provenance[2] != filename)
        ):
            raise _reject(
                "Confirmed self-only file creation is required.", "file_provenance_unverified"
            )
        return provenance

    async def restore_created_file(self, object_id: str, filename: str) -> None:
        """Restore ONLY from a trusted, matching durable confirmed-create receipt.

        This is an internal orchestration hook, never an exposed MCP operation.
        The caller must validate its request fingerprint and self_read_v1 creation
        policy marker before calling. It makes no server-side ACL assertion.
        """
        identifier = _object_id(object_id)
        name = _filename(filename)
        self._remember_created(identifier, name, await self.api.auth.get_bundle())

    async def _request(
        self,
        method: str,
        path: str,
        bundle: CredentialBundle,
        *,
        chat: bool = False,
        json_body: object | None = None,
        data: bytes | None = None,
    ) -> httpx.Response:
        # Callers provide only validated object segments or the constant self route.
        # Never follow even a same-host redirect: its semantics are not established.
        if chat:
            url = f"{self.api._chat_base(bundle)}{path}"
            headers = {"Authentication": f"skypetoken={bundle.skype_token}"}
        else:
            url = f"{_AMS_BASE}{path}"
            headers = {
                "Authorization": f"skype_token {bundle.skype_token}",
                "User-Agent": _AMS_USER_AGENT,
                "x-ms-client-version": _AMS_CLIENT_VERSION,
                "Accept": "*/*"
                if data is not None or path.endswith("/views/original")
                else "application/json",
            }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        mutation = method != "GET"
        try:
            async with self.api._client().stream(
                method,
                url,
                headers=headers,
                json=json_body,
                content=data,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise _reject("File service redirect refused.", "file_redirect_rejected")
                if not response.is_success:
                    code = (
                        "file_operation_ambiguous"
                        if mutation and response.status_code >= 500
                        else f"file_http_{response.status_code}"
                    )
                    raise ApiError(
                        "File service request did not succeed.",
                        code,
                        status_code=response.status_code,
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.api.settings.max_response_bytes:
                        code = "file_operation_ambiguous" if mutation else "file_response_too_large"
                        raise _reject("File service response exceeded its size limit.", code)
                    chunks.append(chunk)
                return httpx.Response(response.status_code, content=b"".join(chunks))
        except httpx.HTTPError:
            code = "file_operation_ambiguous" if mutation else "file_network_error"
            raise _reject("File service request failed; no retry was attempted.", code) from None

    async def create_file(self, filename: str) -> str:
        name = _filename(filename)
        bundle = await self.api.auth.get_bundle()
        _, me = self._identity(bundle)
        response = await self._request(
            "POST",
            "",
            bundle,
            json_body={"type": "sharing/file", "filename": name, "permissions": {me: ["read"]}},
        )
        try:
            if response.status_code != 201:
                raise _reject("Unconfirmed file creation response.", "file_response_invalid")
            value = _json(response).get("id")
            if not isinstance(value, str):
                raise _reject("Missing file object identifier.", "file_object_invalid")
            identifier = _object_id(value)
            self._remember_created(identifier, name, bundle)
            return identifier
        except ApiError:
            raise _reject(
                "File creation returned an unrecognized result; do not retry automatically.",
                "file_operation_ambiguous",
            ) from None

    async def verify_created_file(self, object_id: str) -> None:
        """Verify creation provenance and metadata, not a full server ACL readback."""
        identifier = _object_id(object_id)
        bundle = await self.api.auth.get_bundle()
        await self._verify_created_file(identifier, bundle)

    async def _verify_created_file(self, identifier: str, bundle: CredentialBundle) -> None:
        tenant, me, filename = self._created_file(identifier, bundle)
        metadata = _json(await self._request("GET", f"/{identifier}", bundle))
        if (
            metadata.get("id") != identifier
            or metadata.get("type") != "sharing/file"
            or metadata.get("filename") != filename
            or metadata.get("tenant_id") != tenant
        ):
            raise _reject(
                "Created file metadata does not match its request.", "file_metadata_mismatch"
            )
        # Current AMS metadata omits ACL/owner fields. If supplied, conflicting
        # access information must still invalidate accepted-create provenance.
        if ("permissions" in metadata and metadata["permissions"] != {me: ["read"]}) or any(
            _normalize_mri(metadata[key]) != me for key in ("owner", "creator") if key in metadata
        ):
            raise _reject(
                "File access metadata contradicts self-only creation.", "file_acl_unverified"
            )

    async def upload_file(self, object_id: str, data: bytes) -> None:
        identifier = _object_id(object_id)
        if (
            not isinstance(data, bytes)
            or not data
            or len(data) > self.api.settings.max_response_bytes
        ):
            raise _reject(
                "Attachment content exceeds the supported bounds.", "file_content_invalid"
            )
        bundle = await self.api.auth.get_bundle()
        await self._verify_created_file(identifier, bundle)
        await self._request("PUT", f"/{identifier}/content/original", bundle, data=data)

    async def read_file(self, object_id: str) -> bytes:
        identifier = _object_id(object_id)
        bundle = await self.api.auth.get_bundle()
        self._created_file(identifier, bundle)
        return (await self._request("GET", f"/{identifier}/views/original", bundle)).content

    async def post_file(
        self,
        object_id: str,
        filename: str,
        size: int,
        client_message_id: str,
        *,
        caption: str = "",
    ) -> None:
        identifier = _object_id(object_id)
        name = _filename(filename)
        client_id = _client_id(client_message_id)
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= self.api.settings.max_response_bytes
        ):
            raise _reject("Invalid attachment size.", "file_content_invalid")
        if not isinstance(caption, str) or any(
            (ord(char) < 32 and char not in "\r\n\t") or 0xD800 <= ord(char) <= 0xDFFF
            for char in caption
        ):
            raise _reject("Invalid attachment caption.", "file_caption_invalid")
        bundle = await self.api.auth.get_bundle()
        self._created_file(identifier, bundle, name)
        await self._verify_created_file(identifier, bundle)
        object_url = f"{_AMS_BASE}/{identifier}"
        root = ET.Element("URIObject", {"type": "File.1", "uri": object_url})
        ET.SubElement(root, "Title").text = name
        ET.SubElement(root, "Description").text = caption or name
        ET.SubElement(root, "OriginalName", {"v": name})
        ET.SubElement(root, "FileSize", {"v": str(size)})
        # A private authenticated content reference, never a public sharing link.
        ET.SubElement(root, "a", {"href": f"{object_url}/views/original"}).text = name
        await self._request(
            "POST",
            _SELF_MESSAGES,
            bundle,
            chat=True,
            json_body={
                "content": ET.tostring(root, encoding="unicode"),
                "messagetype": "RichText/Media_GenericFile",
                "contenttype": "text",
                "clientmessageid": client_id,
                "imdisplayname": bundle.display_name,
                "amsreferences": [identifier],
            },
        )

    async def find_file(self, client_message_id: str, object_id: str, filename: str) -> str | None:
        client_id = _client_id(client_message_id)
        identifier = _object_id(object_id)
        name = _filename(filename)
        bundle = await self.api.auth.get_bundle()
        self._created_file(identifier, bundle, name)
        payload = _json(
            await self._request("GET", f"{_SELF_MESSAGES}?pageSize=100", bundle, chat=True)
        )
        messages = payload.get("messages")
        if not isinstance(messages, list) or any(not isinstance(row, dict) for row in messages):
            raise _reject("Self attachment lookup returned invalid data.", "file_lookup_invalid")
        matches = [row for row in messages if row.get("clientmessageid") == client_id]
        if not matches:
            # Bounded absence is not proof a send failed; the caller must not resend.
            return None
        if len(matches) != 1:
            raise _reject("Self attachment lookup is ambiguous.", "file_message_mismatch")
        row = matches[0]
        content = row.get("content")
        if not isinstance(content, str) or "<!" in content:
            raise _reject("Attachment message did not match.", "file_message_mismatch")
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            raise _reject("Attachment message did not match.", "file_message_mismatch") from None
        names = root.findall("OriginalName")
        message_id = row.get("id")
        if (
            row.get("messagetype") != "RichText/Media_GenericFile"
            or root.tag != "URIObject"
            or root.get("type") != "File.1"
            or root.get("uri") != f"{_AMS_BASE}/{identifier}"
            or len(names) != 1
            or names[0].get("v") != name
            or not isinstance(message_id, str)
            or not _MESSAGE_ID.fullmatch(message_id)
        ):
            raise _reject("Attachment message did not match.", "file_message_mismatch")
        return message_id
