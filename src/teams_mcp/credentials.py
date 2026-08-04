from __future__ import annotations

import json
import uuid
from contextlib import suppress
from typing import Protocol

import keyring

from .errors import AuthenticationError
from .models import CredentialBundle

SERVICE_NAME = "ma-nakaya/teams-playwright-mcp-server"
PENDING_SERVICE_NAME = f"{SERVICE_NAME}/pending-login"
SECRET_CHUNK_CHARS = 900


class CredentialStore(Protocol):
    def load(self) -> CredentialBundle | None: ...

    def save(self, bundle: CredentialBundle) -> None: ...

    def delete(self) -> None: ...


class KeyringCredentialStore:
    """Stores all long-lived credentials in the operating-system credential vault."""

    def __init__(self, profile: str) -> None:
        self.profile = profile

    @property
    def _metadata_username(self) -> str:
        return f"{self.profile}:metadata"

    @staticmethod
    def _ensure_secure_backend() -> None:
        backend = keyring.get_keyring()
        try:
            priority = float(backend.priority)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise AuthenticationError(
                "A secure keyring backend is unavailable; refusing plaintext credential storage.",
                "keyring_unavailable",
            ) from exc
        if priority <= 0:
            raise AuthenticationError(
                "A secure keyring backend is unavailable; refusing plaintext credential storage.",
                "keyring_unavailable",
            )

    @staticmethod
    def _get(service: str, username: str) -> str | None:
        KeyringCredentialStore._ensure_secure_backend()
        try:
            return keyring.get_password(service, username)
        except Exception as exc:
            raise AuthenticationError(
                "Could not read the operating-system credential vault.", "keyring_read"
            ) from exc

    @staticmethod
    def _set(service: str, username: str, value: str) -> None:
        KeyringCredentialStore._ensure_secure_backend()
        try:
            keyring.set_password(service, username, value)
        except Exception as exc:
            raise AuthenticationError(
                "Could not write the operating-system credential vault.", "keyring_write"
            ) from exc

    @staticmethod
    def _delete(service: str, username: str) -> None:
        KeyringCredentialStore._ensure_secure_backend()
        try:
            keyring.delete_password(service, username)
        except keyring.errors.PasswordDeleteError:
            return
        except Exception as exc:
            raise AuthenticationError(
                "Could not delete Teams credentials.", "keyring_delete"
            ) from exc

    @staticmethod
    def _chunk_username(profile: str, generation: str, label: str, index: int) -> str:
        return f"{profile}:{generation}:{label}:{index}"

    def _load_chunked_secret(self, metadata: dict[str, object], label: str) -> str:
        try:
            generation = str(metadata["generation"])
            part_count = int(str(metadata[f"{label}_parts"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError(
                "Stored Teams credentials are invalid.", "credentials_invalid"
            ) from exc
        if not generation or not 1 <= part_count <= 64:
            raise AuthenticationError(
                "Stored Teams credentials are invalid.", "credentials_invalid"
            )
        parts: list[str] = []
        for index in range(part_count):
            value = self._get(
                SERVICE_NAME,
                self._chunk_username(self.profile, generation, label, index),
            )
            if value is None:
                raise AuthenticationError(
                    "Stored Teams credentials are incomplete.", "credentials_incomplete"
                )
            parts.append(value)
        return "".join(parts)

    def load(self) -> CredentialBundle | None:
        payload = self._get(SERVICE_NAME, self._metadata_username)
        if payload is None:
            return self._load_legacy()
        try:
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("credential value is not an object")
            if value.get("schema_version") != 2:
                raise ValueError("unsupported credential schema")
            value["refresh_token"] = self._load_chunked_secret(value, "refresh")
            value["skype_token"] = self._load_chunked_secret(value, "skype")
            return CredentialBundle.from_secret_dict(value)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError(
                "Stored Teams credentials are invalid.", "credentials_invalid"
            ) from exc

    def _load_legacy(self) -> CredentialBundle | None:
        payload = self._get(SERVICE_NAME, self.profile)
        if payload is None:
            return None
        try:
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("credential value is not an object")
            return CredentialBundle.from_secret_dict(value)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError(
                "Stored Teams credentials are invalid.", "credentials_invalid"
            ) from exc

    @staticmethod
    def _split(value: str) -> list[str]:
        return [
            value[index : index + SECRET_CHUNK_CHARS]
            for index in range(0, len(value), SECRET_CHUNK_CHARS)
        ]

    def _delete_generation(self, metadata: dict[str, object]) -> None:
        generation = str(metadata.get("generation") or "")
        if not generation:
            return
        for label in ("refresh", "skype"):
            try:
                count = int(str(metadata.get(f"{label}_parts") or 0))
            except ValueError:
                continue
            for index in range(min(max(count, 0), 64)):
                self._delete(
                    SERVICE_NAME,
                    self._chunk_username(self.profile, generation, label, index),
                )

    def save(self, bundle: CredentialBundle) -> None:
        previous_payload = self._get(SERVICE_NAME, self._metadata_username)
        previous: dict[str, object] | None = None
        if previous_payload:
            try:
                parsed = json.loads(previous_payload)
                previous = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                previous = None
        generation = uuid.uuid4().hex
        refresh_parts = self._split(bundle.refresh_token)
        skype_parts = self._split(bundle.skype_token)
        written: list[str] = []
        try:
            for label, parts in (("refresh", refresh_parts), ("skype", skype_parts)):
                for index, part in enumerate(parts):
                    username = self._chunk_username(self.profile, generation, label, index)
                    self._set(SERVICE_NAME, username, part)
                    written.append(username)
            metadata = bundle.to_secret_dict()
            metadata.pop("refresh_token")
            metadata.pop("skype_token")
            metadata.update(
                {
                    "schema_version": 2,
                    "generation": generation,
                    "refresh_parts": len(refresh_parts),
                    "skype_parts": len(skype_parts),
                }
            )
            payload = json.dumps(metadata, separators=(",", ":"), ensure_ascii=True)
            self._set(SERVICE_NAME, self._metadata_username, payload)
        except AuthenticationError:
            for username in written:
                with suppress(AuthenticationError):
                    self._delete(SERVICE_NAME, username)
            raise
        if previous is not None:
            self._delete_generation(previous)
        self._delete(SERVICE_NAME, self.profile)

    def delete(self) -> None:
        payload = self._get(SERVICE_NAME, self._metadata_username)
        if payload:
            try:
                value = json.loads(payload)
                if isinstance(value, dict):
                    self._delete_generation(value)
            except json.JSONDecodeError:
                pass
        self._delete(SERVICE_NAME, self._metadata_username)
        self._delete(SERVICE_NAME, self.profile)


class KeyringPendingLoginStore:
    """Keeps a short-lived device code in the OS vault between CLI invocations."""

    def __init__(self, profile: str) -> None:
        self.profile = profile

    def load(self) -> dict[str, object] | None:
        KeyringCredentialStore._ensure_secure_backend()
        try:
            payload = keyring.get_password(PENDING_SERVICE_NAME, self.profile)
        except Exception as exc:
            raise AuthenticationError(
                "Could not read the pending login from the credential vault.",
                "keyring_read",
            ) from exc
        if payload is None:
            return None
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AuthenticationError(
                "Stored pending login is invalid.", "pending_invalid"
            ) from exc
        if not isinstance(value, dict):
            raise AuthenticationError("Stored pending login is invalid.", "pending_invalid")
        return value

    def save(self, value: dict[str, object]) -> None:
        KeyringCredentialStore._ensure_secure_backend()
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        try:
            keyring.set_password(PENDING_SERVICE_NAME, self.profile, payload)
        except Exception as exc:
            raise AuthenticationError(
                "Could not save the pending login to the credential vault.",
                "keyring_write",
            ) from exc

    def delete(self) -> None:
        KeyringCredentialStore._ensure_secure_backend()
        try:
            keyring.delete_password(PENDING_SERVICE_NAME, self.profile)
        except keyring.errors.PasswordDeleteError:
            return
        except Exception as exc:
            raise AuthenticationError(
                "Could not delete the pending login.", "keyring_delete"
            ) from exc
