from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest

from teams_mcp.api import TeamsApi
from teams_mcp.config import Settings
from teams_mcp.errors import ApiError
from teams_mcp.models import CredentialBundle


class FakeAuth:
    def __init__(self) -> None:
        self.settings = Settings()
        self.bundle = CredentialBundle(
            refresh_token="refresh",
            client_id="client",
            tenant_id="tenant",
            skype_token="skype-secret",
            skype_expires_at=time.time() + 3600,
            region="apac",
        )
        self.refresh_calls = 0

    async def get_bundle(self) -> CredentialBundle:
        return self.bundle

    async def refresh(
        self, known_bundle: CredentialBundle | None = None, *, force: bool = False
    ) -> CredentialBundle:
        del known_bundle, force
        self.refresh_calls += 1
        self.bundle.skype_token = "refreshed-secret"
        return self.bundle

    async def get_csa_token(self, *, force: bool = False) -> str:
        del force
        return "csa-secret"

    async def get_substrate_token(self, *, force: bool = False) -> str:
        del force
        return "substrate-secret"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_rejects_token_delivery_to_unknown_hosts() -> None:
    bundle = FakeAuth().bundle
    with pytest.raises(ApiError, match="unknown chat host"):
        TeamsApi._validate_url("https://evil.example/v1/messages", "chat", bundle)
    with pytest.raises(ApiError, match="unknown transcript host"):
        TeamsApi._validate_url("https://evil.example/transcript", "transcript", bundle)
    with pytest.raises(ApiError, match="Substrate token to an unknown host"):
        TeamsApi._validate_url("https://evil.example/WorkingSetFiles", "substrate", bundle)


@pytest.mark.asyncio
async def test_chat_request_uses_authentication_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authentication"] == "skypetoken=skype-secret"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"conversations": []})

    auth = FakeAuth()
    async with client(handler) as http:
        api = TeamsApi(auth, http=http)  # type: ignore[arg-type]
        assert await api.list_conversations() == []


@pytest.mark.asyncio
async def test_sets_consumption_horizon_with_chat_auth_and_exact_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.headers["Authentication"] == "skypetoken=skype-secret"
        assert request.url.params["name"] == "consumptionhorizon"
        assert "/users/ME/conversations/19%3Achat%40thread.v2/properties" in str(request.url)
        assert request.content == b'{"consumptionhorizon":"1700000000000;1700000000100;0"}'
        return httpx.Response(200)

    auth = FakeAuth()
    async with client(handler) as http:
        api = TeamsApi(auth, http=http)  # type: ignore[arg-type]
        assert (
            await api.set_consumption_horizon(
                "19:chat@thread.v2",
                "1700000000000",
                1700000000100,
            )
            is None
        )


@pytest.mark.asyncio
async def test_401_refreshes_once() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(401)
        assert request.headers["Authentication"] == "skypetoken=refreshed-secret"
        return httpx.Response(200, json={"conversations": []})

    auth = FakeAuth()
    async with client(handler) as http:
        api = TeamsApi(auth, http=http)  # type: ignore[arg-type]
        await api.list_conversations()
    assert auth.refresh_calls == 1
    assert attempts == 2


@pytest.mark.asyncio
async def test_csa_request_uses_resource_bearer_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer csa-secret"
        assert "X-Skypetoken" not in request.headers
        return httpx.Response(200, json={"teams": []})

    auth = FakeAuth()
    async with client(handler) as http:
        api = TeamsApi(auth, http=http)  # type: ignore[arg-type]
        assert await api.teams_snapshot() == {"teams": []}


@pytest.mark.asyncio
async def test_substrate_request_uses_only_substrate_bearer_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer substrate-secret"
        assert "Authentication" not in request.headers
        return httpx.Response(200, json={"value": []})

    auth = FakeAuth()
    async with client(handler) as http:
        api = TeamsApi(auth, http=http)  # type: ignore[arg-type]
        response = await api._json(
            "GET",
            "https://substrate.office.com/api/beta/me/WorkingSetFiles",
            "substrate",
        )
    assert response == {"value": []}


@pytest.mark.asyncio
async def test_transcript_redirect_drops_token_for_blob_storage() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.asm.skype.com":
            return httpx.Response(
                302,
                headers={"Location": "https://safe.blob.core.windows.net/c/vtt?s=signed"},
            )
        return httpx.Response(200, text="WEBVTT")

    auth = FakeAuth()
    async with client(handler) as http:
        api = TeamsApi(auth, http=http)  # type: ignore[arg-type]
        assert await api.transcript_vtt("https://api.asm.skype.com/v1/t") == "WEBVTT"
    assert requests[0].headers["Authorization"] == "skype_token skype-secret"
    assert "Authorization" not in requests[1].headers
