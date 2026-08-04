from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable

import httpx
import pytest

from teams_mcp.auth import CSA_AUDIENCE, SUBSTRATE_AUDIENCE, AuthManager
from teams_mcp.config import Settings
from teams_mcp.models import CredentialBundle


class MemoryStore:
    def __init__(self, bundle: CredentialBundle | None = None) -> None:
        self.bundle = bundle

    def load(self) -> CredentialBundle | None:
        return self.bundle

    def save(self, bundle: CredentialBundle) -> None:
        self.bundle = bundle

    def delete(self) -> None:
        self.bundle = None


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def unsigned_jwt(claims: dict[str, object]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


@pytest.mark.asyncio
async def test_request_device_code_uses_spaces_scope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "login.microsoftonline.com"
        body = request.content.decode()
        assert "api.spaces.skype.com" in body
        return httpx.Response(
            200,
            json={
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": 5,
            },
        )

    async with client(handler) as http:
        auth = AuthManager(Settings(), MemoryStore(), http)
        device_code, prompt = await auth.request_device_code()
    assert device_code == "device-secret"
    assert prompt.user_code == "ABCD-EFGH"
    assert "otc=ABCD-EFGH" in prompt.verification_uri_complete


@pytest.mark.asyncio
async def test_mints_flat_skype_token_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer aad-token"
        return httpx.Response(
            200,
            json={
                "tokens": {"skypeToken": "skype-secret", "expiresIn": 3600},
                "regionGtms": {"region": "apac"},
            },
        )

    async with client(handler) as http:
        auth = AuthManager(Settings(), MemoryStore(), http)
        minted = await auth._mint_skype_token("aad-token")
    assert minted.token == "skype-secret"
    assert minted.region == "apac"
    assert minted.expires_at > time.time()


@pytest.mark.asyncio
async def test_refresh_rotates_secret_without_returning_it_in_status() -> None:
    old = CredentialBundle(
        refresh_token="old-refresh",
        client_id="client",
        tenant_id="tenant",
        skype_token="old-skype",
        skype_expires_at=0,
        region="apac",
    )
    store = MemoryStore(old)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(
                200,
                json={"access_token": "aad", "refresh_token": "new-refresh"},
            )
        return httpx.Response(
            200,
            json={"tokens": {"skypeToken": "new-skype", "expiresIn": 3600}},
        )

    cfg = Settings(credential_profile="pytest-refresh")
    async with client(handler) as http:
        auth = AuthManager(cfg, store, http)
        refreshed = await auth.refresh(old, force=True)
    assert refreshed.refresh_token == "new-refresh"
    assert refreshed.skype_token == "new-skype"
    status = auth.status()
    assert "refresh_token" not in json.dumps(status)
    assert "skype_token" not in json.dumps(status)


@pytest.mark.asyncio
async def test_ensure_status_refreshes_expired_token_without_exposing_secrets() -> None:
    old = CredentialBundle(
        refresh_token="old-refresh",
        client_id="client",
        tenant_id="tenant",
        skype_token="old-skype",
        skype_expires_at=0,
        region="apac",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(
                200,
                json={"access_token": "aad", "refresh_token": "new-refresh"},
            )
        return httpx.Response(
            200,
            json={"tokens": {"skypeToken": "new-skype", "expiresIn": 3600}},
        )

    async with client(handler) as http:
        auth = AuthManager(Settings(), MemoryStore(old), http)
        status = await auth.ensure_status()

    assert status["authenticated"] is True
    assert status["usable"] is True
    assert status["token_expired"] is False
    assert status["token_refreshed"] is True
    assert status["reauthentication_required"] is False
    assert "old-refresh" not in json.dumps(status)
    assert "new-refresh" not in json.dumps(status)
    assert "old-skype" not in json.dumps(status)
    assert "new-skype" not in json.dumps(status)


@pytest.mark.asyncio
async def test_ensure_status_requires_login_when_credentials_are_missing() -> None:
    async with client(lambda _: httpx.Response(500)) as http:
        auth = AuthManager(Settings(), MemoryStore(), http)
        status = await auth.ensure_status()

    assert status == {
        "authenticated": False,
        "profile": "default",
        "usable": False,
        "token_refreshed": False,
        "reauthentication_required": True,
    }


@pytest.mark.asyncio
async def test_csa_token_has_expected_audience_and_rotates_refresh_token() -> None:
    old = CredentialBundle(
        refresh_token="old-refresh",
        client_id="client",
        tenant_id="tenant",
        skype_token="skype",
        skype_expires_at=time.time() + 3_600,
        region="apac",
    )
    store = MemoryStore(old)
    token = unsigned_jwt({"aud": CSA_AUDIENCE, "exp": int(time.time()) + 3_600})

    def handler(request: httpx.Request) -> httpx.Response:
        assert "chatsvcagg.teams.microsoft.com" in request.content.decode()
        return httpx.Response(
            200,
            json={"access_token": token, "refresh_token": "rotated-refresh"},
        )

    async with client(handler) as http:
        auth = AuthManager(Settings(credential_profile="pytest-csa"), store, http)
        assert await auth.get_csa_token() == token
        assert await auth.get_csa_token() == token
    assert store.bundle is not None
    assert store.bundle.refresh_token == "rotated-refresh"


@pytest.mark.asyncio
async def test_substrate_token_uses_expected_scope_and_audience() -> None:
    old = CredentialBundle(
        refresh_token="refresh",
        client_id="client",
        tenant_id="tenant",
        skype_token="skype",
        skype_expires_at=time.time() + 3_600,
        region="apac",
    )
    token = unsigned_jwt({"aud": SUBSTRATE_AUDIENCE, "exp": int(time.time()) + 3_600})

    def handler(request: httpx.Request) -> httpx.Response:
        assert "substrate.office.com" in request.content.decode()
        return httpx.Response(200, json={"access_token": token})

    async with client(handler) as http:
        auth = AuthManager(Settings(credential_profile="pytest-substrate"), MemoryStore(old), http)
        assert await auth.get_substrate_token() == token
