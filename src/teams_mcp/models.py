from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ChatKind = Literal["direct", "group", "meeting", "self", "unknown"]
ReadState = Literal["read", "unread", "unknown"]
ReadStateSource = Literal["csa", "unavailable"]


@dataclass(slots=True)
class CredentialBundle:
    refresh_token: str
    client_id: str
    tenant_id: str
    skype_token: str
    skype_expires_at: float
    region: str
    display_name: str = "Teams User"

    def to_secret_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_secret_dict(cls, value: dict[str, Any]) -> CredentialBundle:
        return cls(
            refresh_token=str(value["refresh_token"]),
            client_id=str(value["client_id"]),
            tenant_id=str(value["tenant_id"]),
            skype_token=str(value["skype_token"]),
            skype_expires_at=float(value["skype_expires_at"]),
            region=str(value["region"]),
            display_name=str(value.get("display_name") or "Teams User"),
        )


@dataclass(slots=True)
class Chat:
    id: str
    kind: ChatKind
    topic: str | None
    display_name: str | None
    member_count: int | None
    last_message_at: str | None
    last_message_preview: str | None
    read_state: ReadState = "unknown"
    read_state_source: ReadStateSource = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Message:
    id: str
    conversation_id: str
    message_type: str
    sender_id: str | None
    sender_display_name: str | None
    content_text: str
    compose_time: str | None
    original_arrival_time: str | None
    root_message_id: str | None = None
    parent_message_id: str | None = None
    subject: str | None = None
    is_deleted: bool = False
    attachments: list[dict[str, str | None]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Team:
    id: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Channel:
    id: str
    team_id: str
    name: str
    description: str | None = None
    read_state: ReadState = "unknown"
    important_message_read_state: ReadState = "unknown"
    read_state_source: ReadStateSource = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Thread:
    root: Message
    replies: list[Message] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"root": self.root.to_dict(), "replies": [item.to_dict() for item in self.replies]}


@dataclass(slots=True)
class TranscriptEntry:
    speaker: str
    start_time: str
    end_time: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
