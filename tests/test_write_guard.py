from __future__ import annotations

import pytest

from teams_mcp.config import Settings
from teams_mcp.errors import WriteRejectedError
from teams_mcp.write_guard import ReadStateWriteGuard, WriteGuard


def settings(*, enabled: bool, allowlist: frozenset[str]) -> Settings:
    return Settings(writes_enabled=enabled, write_allowlist=allowlist, agent_marker="[BOT]")


def test_writes_are_disabled_by_default() -> None:
    guard = WriteGuard(settings(enabled=False, allowlist=frozenset({"chat:abc"})))
    with pytest.raises(WriteRejectedError, match="disabled"):
        guard.authorize(target="chat:abc", content="hello", confirm=True)


def test_target_must_be_exactly_allowlisted() -> None:
    guard = WriteGuard(settings(enabled=True, allowlist=frozenset({"chat:abc"})))
    with pytest.raises(WriteRejectedError, match="not allowlisted"):
        guard.authorize(target="chat:abcd", content="hello", confirm=True)


def test_each_write_requires_confirmation() -> None:
    guard = WriteGuard(settings(enabled=True, allowlist=frozenset({"chat:abc"})))
    with pytest.raises(WriteRejectedError, match="confirm=true"):
        guard.authorize(target="chat:abc", content="hello", confirm=False)


def test_authorized_write_gets_marker_and_digest() -> None:
    guard = WriteGuard(settings(enabled=True, allowlist=frozenset({"chat:abc"})))
    result = guard.authorize(target="chat:abc", content=" hello ", confirm=True)
    assert result.content == "[BOT] hello"
    assert len(result.content_sha256) == 64


def read_state_settings(*, enabled: bool, allowlist: frozenset[str]) -> Settings:
    return Settings(
        read_state_writes_enabled=enabled,
        read_state_allowlist=allowlist,
    )


def test_read_state_writes_are_disabled_by_default() -> None:
    guard = ReadStateWriteGuard(
        read_state_settings(enabled=False, allowlist=frozenset({"chat:abc"}))
    )
    with pytest.raises(WriteRejectedError, match="disabled"):
        guard.authorize(target="chat:abc", confirm=True)


def test_read_state_target_must_be_exactly_allowlisted() -> None:
    guard = ReadStateWriteGuard(
        read_state_settings(enabled=True, allowlist=frozenset({"chat:abc"}))
    )
    with pytest.raises(WriteRejectedError, match="not allowlisted"):
        guard.authorize(target="chat:abcd", confirm=True)


def test_read_state_write_requires_per_call_confirmation() -> None:
    guard = ReadStateWriteGuard(
        read_state_settings(enabled=True, allowlist=frozenset({"chat:abc"}))
    )
    with pytest.raises(WriteRejectedError, match="confirm=true"):
        guard.authorize(target="chat:abc", confirm=False)
