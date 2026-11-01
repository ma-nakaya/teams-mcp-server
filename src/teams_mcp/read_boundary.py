from __future__ import annotations

from typing import Any

from .errors import TeamsMcpError
from .parsers import _record


def _message_id(value: object) -> str | None:
    normalized = str(value).strip() if value is not None else ""
    if not normalized or not normalized.isdecimal() or len(normalized) > 32:
        return None
    return normalized


def _horizon_message_id(raw: dict[str, Any]) -> str | None:
    horizon = _record(raw.get("consumptionHorizon") or raw.get("userConsumptionHorizon"))
    return _message_id(horizon.get("originalArrivalTime"))


def resolve_read_boundary(
    raw: dict[str, Any],
    *,
    through_message_id: str | None,
    through_latest: bool,
) -> tuple[str, str | None]:
    explicit = _message_id(through_message_id)
    if through_message_id is not None and explicit is None:
        raise TeamsMcpError(
            "through_message_id must be a decimal Teams message ID of at most 32 digits.",
            "read_boundary_invalid",
        )
    if (explicit is None) == (not through_latest):
        raise TeamsMcpError(
            "Specify exactly one of through_message_id or through_latest=true.",
            "read_boundary_required",
        )
    latest = _message_id(_record(raw.get("lastMessage")).get("id"))
    if latest is None:
        raise TeamsMcpError(
            "Teams did not provide a usable latest-message boundary for this target.",
            "latest_message_unavailable",
        )
    boundary = latest if through_latest else explicit
    if boundary is None:
        raise TeamsMcpError("The read boundary is missing.", "read_boundary_required")
    if int(boundary) > int(latest):
        raise TeamsMcpError(
            "through_message_id is newer than the target's latest message.",
            "read_boundary_after_latest",
        )
    return boundary, _horizon_message_id(raw)
