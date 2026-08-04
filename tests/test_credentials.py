from __future__ import annotations

import time

import pytest

from teams_mcp import credentials
from teams_mcp.credentials import SERVICE_NAME, KeyringCredentialStore
from teams_mcp.errors import AuthenticationError
from teams_mcp.models import CredentialBundle


class SecureBackend:
    priority = 5


def install_memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    vault: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: SecureBackend())
    monkeypatch.setattr(
        credentials.keyring,
        "get_password",
        lambda service, username: vault.get((service, username)),
    )
    monkeypatch.setattr(
        credentials.keyring,
        "set_password",
        lambda service, username, password: vault.__setitem__((service, username), password),
    )
    monkeypatch.setattr(
        credentials.keyring,
        "delete_password",
        lambda service, username: vault.pop((service, username), None),
    )
    return vault


def long_bundle() -> CredentialBundle:
    return CredentialBundle(
        refresh_token="r" * 3_200,
        client_id="client",
        tenant_id="tenant",
        skype_token="s" * 4_100,
        skype_expires_at=time.time() + 3_600,
        region="apac",
        display_name="Test User",
    )


def test_large_tokens_are_chunked_and_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = install_memory_keyring(monkeypatch)
    store = KeyringCredentialStore("test")
    expected = long_bundle()
    store.save(expected)

    assert store.load() == expected
    stored_values = [value for (service, _), value in vault.items() if service == SERVICE_NAME]
    assert all(len(value) <= 900 for value in stored_values)
    assert expected.refresh_token not in stored_values
    assert expected.skype_token not in stored_values


def test_delete_removes_chunked_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = install_memory_keyring(monkeypatch)
    store = KeyringCredentialStore("test")
    store.save(long_bundle())
    store.delete()
    assert store.load() is None
    assert not vault


def test_backend_write_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    install_memory_keyring(monkeypatch)

    def fail(*_: object) -> None:
        raise OSError(1783, "raw operating-system error")

    monkeypatch.setattr(credentials.keyring, "set_password", fail)
    with pytest.raises(AuthenticationError, match="credential vault") as error:
        KeyringCredentialStore("test").save(long_bundle())
    assert error.value.code == "keyring_write"
