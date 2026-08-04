from __future__ import annotations

import asyncio
import base64
import json
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from filelock import FileLock

from .config import Settings
from .credentials import CredentialStore, KeyringCredentialStore
from .errors import AuthenticationError
from .models import CredentialBundle

AUTHORITY = "https://login.microsoftonline.com"
ORGANIZATIONS = "organizations"
DEVICE_SCOPE = "https://api.spaces.skype.com/.default openid profile offline_access"
CSA_SCOPE = "https://chatsvcagg.teams.microsoft.com/.default offline_access"
CSA_AUDIENCE = "https://chatsvcagg.teams.microsoft.com"
SUBSTRATE_SCOPE = "https://substrate.office.com/.default offline_access"
SUBSTRATE_AUDIENCE = "https://substrate.office.com"
AUTHZ_URL = "https://teams.microsoft.com/api/authsvc/v1.0/authz"
TENANTS_URL = "https://teams.microsoft.com/api/mt/emea/beta/users/tenants"
PLACEHOLDER_TENANTS = {
    ORGANIZATIONS,
    "9188040d-6c67-4c5b-b112-36a304b66dad",
    "f8cdef31-a31e-4b4a-93e4-5f571e91255a",
}
DEFAULT_SKYPE_TTL_SECONDS = 21_600
REFRESH_EARLY_SECONDS = 300


@dataclass(frozen=True, slots=True)
class DeviceCodePrompt:
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class MintedSkypeToken:
    token: str
    expires_at: float
    region: str | None


@dataclass(frozen=True, slots=True)
class CachedBearerToken:
    token: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class PendingDeviceLogin:
    device_code: str
    prompt: DeviceCodePrompt
    client_id: str
    expires_at: float

    def to_secret_dict(self) -> dict[str, object]:
        return {
            "device_code": self.device_code,
            "user_code": self.prompt.user_code,
            "verification_uri": self.prompt.verification_uri,
            "verification_uri_complete": self.prompt.verification_uri_complete,
            "interval": self.prompt.interval,
            "client_id": self.client_id,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_secret_dict(cls, value: dict[str, object]) -> PendingDeviceLogin:
        try:
            expires_at = float(str(value["expires_at"]))
            remaining = max(int(expires_at - time.time()), 0)
            prompt = DeviceCodePrompt(
                user_code=str(value["user_code"]),
                verification_uri=str(value["verification_uri"]),
                verification_uri_complete=str(value["verification_uri_complete"]),
                expires_in=remaining,
                interval=max(int(str(value["interval"])), 1),
            )
            return cls(
                device_code=str(value["device_code"]),
                prompt=prompt,
                client_id=str(value["client_id"]),
                expires_at=expires_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError(
                "Stored pending login is invalid.", "pending_invalid"
            ) from exc


def _jwt_claims(token: str) -> dict[str, Any]:
    """Decode non-security JWT metadata. Signature validation remains Microsoft's job."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        value = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _aad_error(body: object, status: int) -> str:
    if isinstance(body, dict):
        raw = body.get("error_description") or body.get("error")
        if isinstance(raw, str):
            return raw.splitlines()[0][:500]
    return f"HTTP {status}"


class AuthManager:
    def __init__(
        self,
        settings: Settings | None = None,
        store: CredentialStore | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.settings.validate()
        self.store = store or KeyringCredentialStore(self.settings.credential_profile)
        self._http = http
        self._owned_http: httpx.AsyncClient | None = None
        self._process_lock = asyncio.Lock()
        self._resource_tokens: dict[str, CachedBearerToken] = {}

    async def __aenter__(self) -> AuthManager:
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
    def _device_url() -> str:
        return f"{AUTHORITY}/{ORGANIZATIONS}/oauth2/v2.0/devicecode"

    @staticmethod
    def _token_url(tenant_id: str) -> str:
        return f"{AUTHORITY}/{tenant_id}/oauth2/v2.0/token"

    async def request_device_code(self) -> tuple[str, DeviceCodePrompt]:
        response = await self._client().post(
            self._device_url(),
            data={"client_id": self.settings.client_id, "scope": DEVICE_SCOPE},
        )
        body = self._json(response)
        if response.status_code != 200 or not isinstance(body, dict):
            raise AuthenticationError(
                f"Microsoft device-code request failed: {_aad_error(body, response.status_code)}",
                "device_code_failed",
            )
        device_code = str(body.get("device_code") or "")
        user_code = str(body.get("user_code") or "")
        verification_uri = str(body.get("verification_uri") or "https://microsoft.com/devicelogin")
        complete = str(
            body.get("verification_uri_complete")
            or f"{verification_uri}?{httpx.QueryParams({'otc': user_code})}"
        )
        if not device_code or not user_code:
            raise AuthenticationError(
                "Microsoft returned an incomplete device code.", "device_code_invalid"
            )
        return device_code, DeviceCodePrompt(
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=complete,
            expires_in=int(body.get("expires_in") or 900),
            interval=max(int(body.get("interval") or 5), 1),
        )

    async def login(
        self,
        *,
        open_browser: bool = True,
        on_code: Callable[[DeviceCodePrompt], None] | None = None,
    ) -> CredentialBundle:
        pending = await self.begin_login(open_browser=open_browser, on_code=on_code)
        return await self.complete_login(pending)

    async def begin_login(
        self,
        *,
        open_browser: bool = True,
        on_code: Callable[[DeviceCodePrompt], None] | None = None,
    ) -> PendingDeviceLogin:
        device_code, prompt = await self.request_device_code()
        if on_code is not None:
            on_code(prompt)
        if open_browser:
            webbrowser.open(prompt.verification_uri_complete, new=2)
        return PendingDeviceLogin(
            device_code=device_code,
            prompt=prompt,
            client_id=self.settings.client_id,
            expires_at=time.time() + prompt.expires_in,
        )

    async def complete_login(self, pending: PendingDeviceLogin) -> CredentialBundle:
        if pending.expires_at <= time.time():
            raise AuthenticationError("The Microsoft device code expired.", "device_code_expired")
        initial = await self._poll_device_token(pending.device_code, pending.prompt)
        refresh_token = str(initial.get("refresh_token") or "")
        access_token = str(initial.get("access_token") or "")
        if not refresh_token or not access_token:
            raise AuthenticationError(
                "Sign-in did not return the refresh token needed for Teams.",
                "refresh_token_missing",
            )
        tenant_id = await self._resolve_tenant(access_token)
        scoped = await self._exchange_refresh_token(refresh_token, tenant_id, pending.client_id)
        minted = await self._mint_skype_token(str(scoped["access_token"]))
        bundle = CredentialBundle(
            refresh_token=str(scoped.get("refresh_token") or refresh_token),
            client_id=pending.client_id,
            tenant_id=tenant_id,
            skype_token=minted.token,
            skype_expires_at=minted.expires_at,
            region=minted.region or self.settings.region_fallback,
        )
        self.store.save(bundle)
        return bundle

    async def _poll_device_token(
        self, device_code: str, prompt: DeviceCodePrompt
    ) -> dict[str, Any]:
        deadline = time.monotonic() + prompt.expires_in
        interval = prompt.interval
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            response = await self._client().post(
                self._token_url(ORGANIZATIONS),
                data={
                    "client_id": self.settings.client_id,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                },
            )
            body = self._json(response)
            if response.status_code == 200 and isinstance(body, dict):
                return body
            error = body.get("error") if isinstance(body, dict) else None
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "authorization_declined":
                raise AuthenticationError("Microsoft sign-in was declined.", "device_code_declined")
            if error == "expired_token":
                break
            raise AuthenticationError(
                f"Microsoft device authorization failed: {_aad_error(body, response.status_code)}",
                "device_code_failed",
            )
        raise AuthenticationError("The Microsoft device code expired.", "device_code_expired")

    async def _resolve_tenant(self, access_token: str) -> str:
        tenant_id = _jwt_claims(access_token).get("tid")
        if isinstance(tenant_id, str) and tenant_id not in PLACEHOLDER_TENANTS:
            return tenant_id
        response = await self._client().get(
            TENANTS_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        body = self._json(response)
        if response.status_code != 200 or not isinstance(body, list):
            raise AuthenticationError(
                f"Teams tenant discovery failed: HTTP {response.status_code}",
                "tenant_discovery_failed",
            )
        candidates = [item for item in body if isinstance(item, dict) and item.get("tenantId")]
        redeemed = next(
            (item for item in candidates if item.get("isInvitationRedeemed") is not False),
            None,
        )
        if redeemed is None:
            raise AuthenticationError(
                "No redeemed Teams tenant is associated with this account.",
                "tenant_missing",
            )
        return str(redeemed["tenantId"])

    async def _exchange_refresh_token(
        self,
        refresh_token: str,
        tenant_id: str,
        client_id: str,
        *,
        scope: str = DEVICE_SCOPE,
    ) -> dict[str, Any]:
        response = await self._client().post(
            self._token_url(tenant_id),
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": scope,
            },
            headers={"Origin": "https://teams.microsoft.com"},
        )
        body = self._json(response)
        if (
            response.status_code != 200
            or not isinstance(body, dict)
            or not body.get("access_token")
        ):
            raise AuthenticationError(
                f"Microsoft token refresh failed: {_aad_error(body, response.status_code)}",
                "token_refresh_failed",
            )
        return body

    async def get_csa_token(self, *, force: bool = False) -> str:
        return await self._get_resource_token(
            scope=CSA_SCOPE,
            audience=CSA_AUDIENCE,
            force=force,
        )

    async def get_substrate_token(self, *, force: bool = False) -> str:
        return await self._get_resource_token(
            scope=SUBSTRATE_SCOPE,
            audience=SUBSTRATE_AUDIENCE,
            force=force,
        )

    async def _get_resource_token(
        self,
        *,
        scope: str,
        audience: str,
        force: bool,
    ) -> str:
        cached = self._resource_tokens.get(audience)
        if (
            not force
            and cached is not None
            and cached.expires_at - time.time() > REFRESH_EARLY_SECONDS
        ):
            return cached.token
        async with self._process_lock:
            cached = self._resource_tokens.get(audience)
            if (
                not force
                and cached is not None
                and cached.expires_at - time.time() > REFRESH_EARLY_SECONDS
            ):
                return cached.token
            with FileLock(self.settings.lock_path, timeout=30):
                bundle = self.store.load()
                if bundle is None:
                    raise AuthenticationError("Teams is not authenticated.", "not_authenticated")
                token_response = await self._exchange_refresh_token(
                    bundle.refresh_token,
                    bundle.tenant_id,
                    bundle.client_id,
                    scope=scope,
                )
                access_token = str(token_response["access_token"])
                claims = _jwt_claims(access_token)
                if claims.get("aud") != audience:
                    raise AuthenticationError(
                        "Microsoft returned a token for an unexpected resource audience.",
                        "token_audience_mismatch",
                    )
                rotated = str(token_response.get("refresh_token") or bundle.refresh_token)
                if rotated != bundle.refresh_token:
                    bundle.refresh_token = rotated
                    self.store.save(bundle)
                expires_at = float(
                    claims.get("exp")
                    or time.time() + int(token_response.get("expires_in") or 3_600)
                )
                self._resource_tokens[audience] = CachedBearerToken(access_token, expires_at)
                return access_token

    async def _mint_skype_token(self, access_token: str) -> MintedSkypeToken:
        response = await self._client().post(
            AUTHZ_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json; ver=1.0",
            },
        )
        body = self._json(response)
        if response.status_code != 200 or not isinstance(body, dict):
            raise AuthenticationError(
                f"Teams token minting failed: {_aad_error(body, response.status_code)}",
                "skype_token_failed",
            )
        nested_value = body.get("skypeToken")
        nested: dict[str, Any] = nested_value if isinstance(nested_value, dict) else {}
        flat_value = body.get("tokens")
        flat: dict[str, Any] = flat_value if isinstance(flat_value, dict) else {}
        token = nested.get("skypetoken") or flat.get("skypeToken")
        ttl = (
            nested.get("skypeTokenExpiresIn") or flat.get("expiresIn") or DEFAULT_SKYPE_TTL_SECONDS
        )
        region_value = body.get("regionGtms")
        region_info: dict[str, Any] = region_value if isinstance(region_value, dict) else {}
        region = region_info.get("region") or region_info.get("middleTier") or region_info.get("mt")
        if not isinstance(token, str) or not token:
            raise AuthenticationError(
                "Teams auth response had no Skype token.", "skype_token_missing"
            )
        if region not in {"amer", "emea", "apac"}:
            region = None
        return MintedSkypeToken(
            token=token,
            expires_at=time.time() + int(ttl),
            region=region if isinstance(region, str) else None,
        )

    async def get_bundle(self) -> CredentialBundle:
        bundle = self.store.load()
        if bundle is None:
            raise AuthenticationError(
                "Teams is not authenticated. Run `teams-mcp login` first.",
                "not_authenticated",
            )
        if bundle.skype_expires_at - time.time() > REFRESH_EARLY_SECONDS:
            return bundle
        return await self.refresh(bundle)

    async def refresh(
        self, known_bundle: CredentialBundle | None = None, *, force: bool = False
    ) -> CredentialBundle:
        async with self._process_lock:
            with FileLock(self.settings.lock_path, timeout=30):
                latest = self.store.load()
                bundle = latest or known_bundle
                if bundle is None:
                    raise AuthenticationError("Teams is not authenticated.", "not_authenticated")
                if (
                    not force
                    and latest is not None
                    and latest.skype_expires_at - time.time() > REFRESH_EARLY_SECONDS
                ):
                    return latest
                scoped = await self._exchange_refresh_token(
                    bundle.refresh_token, bundle.tenant_id, bundle.client_id
                )
                minted = await self._mint_skype_token(str(scoped["access_token"]))
                refreshed = CredentialBundle(
                    refresh_token=str(scoped.get("refresh_token") or bundle.refresh_token),
                    client_id=bundle.client_id,
                    tenant_id=bundle.tenant_id,
                    skype_token=minted.token,
                    skype_expires_at=minted.expires_at,
                    region=minted.region or bundle.region,
                    display_name=bundle.display_name,
                )
                self.store.save(refreshed)
                return refreshed

    def status(self) -> dict[str, object]:
        bundle = self.store.load()
        if bundle is None:
            return {"authenticated": False, "profile": self.settings.credential_profile}
        return {
            "authenticated": True,
            "profile": self.settings.credential_profile,
            "tenant_id": bundle.tenant_id,
            "region": bundle.region,
            "display_name": bundle.display_name,
            "token_expires_at": bundle.skype_expires_at,
            "token_expired": bundle.skype_expires_at <= time.time(),
        }

    async def ensure_status(self) -> dict[str, object]:
        """Return a usable auth status, refreshing an expired token when possible."""
        before = self.status()
        if not before.get("authenticated"):
            return {
                **before,
                "usable": False,
                "token_refreshed": False,
                "reauthentication_required": True,
            }

        previous_expiry = before.get("token_expires_at")
        try:
            await self.get_bundle()
        except AuthenticationError as exc:
            return {
                **self.status(),
                "usable": False,
                "token_refreshed": False,
                "reauthentication_required": True,
                "refresh_error_code": exc.code,
            }

        current = self.status()
        return {
            **current,
            "usable": not bool(current.get("token_expired")),
            "token_refreshed": current.get("token_expires_at") != previous_expiry,
            "reauthentication_required": False,
        }

    def logout(self) -> None:
        self.store.delete()

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except json.JSONDecodeError:
            return {}
