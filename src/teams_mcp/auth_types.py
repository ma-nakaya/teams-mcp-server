from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

from .errors import AuthenticationError


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
