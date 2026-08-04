from __future__ import annotations

import os
from dataclasses import dataclass, field

from platformdirs import user_cache_path

DEFAULT_WORK_CLIENT_ID = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _csv_env(name: str) -> frozenset[str]:
    value = os.getenv(name, "")
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    credential_profile: str = field(
        default_factory=lambda: (
            os.getenv("TEAMS_CREDENTIAL_PROFILE", "default").strip() or "default"
        )
    )
    client_id: str = field(
        default_factory=lambda: (
            os.getenv("TEAMS_CLIENT_ID", DEFAULT_WORK_CLIENT_ID).strip() or DEFAULT_WORK_CLIENT_ID
        )
    )
    region_fallback: str = field(
        default_factory=lambda: os.getenv("TEAMS_REGION", "apac").strip().lower() or "apac"
    )
    writes_enabled: bool = field(default_factory=lambda: _bool_env("TEAMS_WRITES_ENABLED", False))
    write_allowlist: frozenset[str] = field(
        default_factory=lambda: _csv_env("TEAMS_WRITE_ALLOWLIST")
    )
    read_state_writes_enabled: bool = field(
        default_factory=lambda: _bool_env("TEAMS_READ_STATE_WRITES_ENABLED", False)
    )
    read_state_allowlist: frozenset[str] = field(
        default_factory=lambda: _csv_env("TEAMS_READ_STATE_ALLOWLIST")
    )
    agent_marker: str = field(
        default_factory=lambda: os.getenv("TEAMS_AGENT_MARKER", "[MCP]").strip()
    )
    request_timeout_seconds: int = field(
        default_factory=lambda: _int_env("TEAMS_REQUEST_TIMEOUT_SECONDS", 30)
    )
    max_response_bytes: int = field(
        default_factory=lambda: _int_env("TEAMS_MAX_RESPONSE_BYTES", 10_000_000)
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("TEAMS_LOG_LEVEL", "INFO").strip().upper()
    )

    @property
    def lock_path(self) -> str:
        root = user_cache_path("teams-playwright-mcp-server", "ma-nakaya", ensure_exists=True)
        return str(root / f"auth-{self.credential_profile}.lock")

    def validate(self) -> None:
        if self.region_fallback not in {"amer", "emea", "apac"}:
            raise ValueError("TEAMS_REGION must be amer, emea, or apac")
        if self.request_timeout_seconds < 1:
            raise ValueError("TEAMS_REQUEST_TIMEOUT_SECONDS must be positive")
        if self.max_response_bytes < 1024:
            raise ValueError("TEAMS_MAX_RESPONSE_BYTES must be at least 1024")
