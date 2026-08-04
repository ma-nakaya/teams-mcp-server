from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from .config import Settings
from .errors import WriteRejectedError

logger = logging.getLogger("teams_mcp.audit")


@dataclass(frozen=True, slots=True)
class AuthorizedWrite:
    target: str
    content: str
    content_sha256: str


class WriteGuard:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def authorize(self, *, target: str, content: str, confirm: bool) -> AuthorizedWrite:
        if not self.settings.writes_enabled:
            raise WriteRejectedError(
                "Writes are disabled. Set TEAMS_WRITES_ENABLED=true after reviewing the target.",
                "writes_disabled",
            )
        if target not in self.settings.write_allowlist:
            raise WriteRejectedError(
                f"Write target is not allowlisted: {target}",
                "target_not_allowlisted",
            )
        if not confirm:
            raise WriteRejectedError(
                "This write requires confirm=true on the individual MCP call.",
                "confirmation_required",
            )
        normalized = content.strip()
        if not normalized:
            raise WriteRejectedError("Message content cannot be empty.", "content_empty")
        if len(normalized) > 4_000:
            raise WriteRejectedError(
                "Message content exceeds 4,000 characters.", "content_too_long"
            )
        marker = self.settings.agent_marker
        marked = (
            f"{marker} {normalized}" if marker and not normalized.startswith(marker) else normalized
        )
        digest = hashlib.sha256(marked.encode("utf-8")).hexdigest()
        logger.info(
            "teams_write_authorized target=%s length=%d sha256=%s", target, len(marked), digest
        )
        return AuthorizedWrite(target=target, content=marked, content_sha256=digest)
