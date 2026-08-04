from __future__ import annotations

from typing import Any

import pytest

from teams_mcp.config import Settings
from teams_mcp.errors import TeamsMcpError
from teams_mcp.service import TeamsService


class FakeAuth:
    settings = Settings()

    async def ensure_status(self) -> dict[str, object]:
        return {"authenticated": True, "usable": True}


class FakeApi:
    def __init__(self) -> None:
        self.read_state_writes: list[tuple[str, str, int]] = []

    async def list_conversations(self, page_size: int = 500) -> list[dict[str, Any]]:
        del page_size
        return [
            {
                "id": "19:direct",
                "threadProperties": {"memberCount": "2"},
                "lastMessage": {"content": "<p>hello</p>", "composetime": "2"},
            },
            {
                "id": "19:meeting_abc@thread.v2",
                "threadProperties": {"topic": "Weekly"},
                "lastMessage": {"content": "meeting", "composetime": "3"},
            },
            {
                "id": "19:channel",
                "threadProperties": {"groupId": "team-1", "spaceThreadTopic": "Team One"},
            },
        ]

    async def message_pages(self, *_: object, **__: object) -> Any:
        yield [
            {
                "id": "recording",
                "messagetype": "RichText/Media_CallRecording",
                "content": (
                    '<RecordingStatus status="Success" />'
                    '<OriginalName v="Weekly" />'
                    '<item type="amsTranscript" uri="https://api.asm.skype.com/vtt" />'
                ),
            }
        ]

    async def transcript_vtt(self, url: str) -> str:
        assert url == "https://api.asm.skype.com/vtt"
        return "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Alice>Hello</v>\n"

    async def teams_snapshot(self, *, force: bool = False) -> dict[str, object]:
        del force
        return {
            "chats": [
                {
                    "id": "19:direct",
                    "isRead": False,
                    "consumptionHorizon": {"originalArrivalTime": 1},
                    "lastMessage": {"id": "2"},
                },
                {
                    "id": "19:meeting_abc@thread.v2",
                    "isRead": True,
                    "consumptionHorizon": {"originalArrivalTime": 3},
                    "lastMessage": {"id": "3"},
                },
            ],
            "teams": [
                {
                    "id": "team-1",
                    "displayName": "Team One",
                    "channels": [
                        {
                            "id": "channel-1",
                            "displayName": "General",
                            "description": "General channel",
                            "isMessageRead": False,
                            "isImportantMessageRead": True,
                            "consumptionHorizon": {"originalArrivalTime": 1},
                            "lastMessage": {"id": "2"},
                        }
                    ],
                }
            ],
        }

    async def set_consumption_horizon(
        self,
        conversation_id: str,
        last_read_message_id: str,
        read_time_ms: int,
    ) -> object:
        self.read_state_writes.append((conversation_id, last_read_message_id, read_time_ms))
        return None

    async def channel_messages(self, *_: object, **__: object) -> object:
        return [
            {"id": "root-1", "content": "Root", "composetime": "1"},
            {
                "id": "reply-in-root-list",
                "content": "Reply",
                "rootMessageId": "root-1",
            },
        ]

    async def channel_replies(self, *_: object, **__: object) -> object:
        return [
            {
                "id": "reply-1",
                "content": "Reply",
                "rootMessageId": "root-1",
                "composetime": "2",
            }
        ]


def service() -> TeamsService:
    return TeamsService(FakeAuth(), FakeApi())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_auth_status_reports_usable_session() -> None:
    assert await service().auth_status() == {"authenticated": True, "usable": True}


@pytest.mark.asyncio
async def test_lists_meeting_chats_and_teams_separately() -> None:
    subject = service()
    meetings = await subject.list_meeting_chats()
    teams = await subject.list_teams()
    assert meetings["count"] == 1
    assert meetings["chats"][0]["id"] == "19:meeting_abc@thread.v2"  # type: ignore[index]
    assert meetings["chats"][0]["read_state"] == "read"  # type: ignore[index]
    assert teams == {"count": 1, "teams": [{"id": "team-1", "name": "Team One"}]}


@pytest.mark.asyncio
async def test_filters_unread_chats_using_csa_state() -> None:
    chats = await service().list_recent_chats(unread_only=True)
    assert chats["count"] == 1
    assert chats["unread_only"] is True
    assert chats["chats"][0]["id"] == "19:direct"  # type: ignore[index]
    assert chats["chats"][0]["read_state"] == "unread"  # type: ignore[index]
    assert chats["chats"][0]["read_state_source"] == "csa"  # type: ignore[index]


@pytest.mark.asyncio
async def test_lists_channels_from_csa_snapshot() -> None:
    channels = await service().list_channels("team-1")
    assert channels == {
        "team_id": "team-1",
        "count": 1,
        "unread_only": False,
        "channels": [
            {
                "id": "channel-1",
                "team_id": "team-1",
                "name": "General",
                "description": "General channel",
                "read_state": "unread",
                "important_message_read_state": "read",
                "read_state_source": "csa",
            }
        ],
    }


@pytest.mark.asyncio
async def test_filters_unread_channels_using_csa_state() -> None:
    channels = await service().list_channels("team-1", unread_only=True)
    assert channels["count"] == 1
    assert channels["unread_only"] is True
    assert channels["channels"][0]["read_state"] == "unread"  # type: ignore[index]


def read_state_settings(*targets: str) -> Settings:
    return Settings(
        read_state_writes_enabled=True,
        read_state_allowlist=frozenset(targets),
    )


@pytest.mark.asyncio
async def test_marks_chat_read_to_explicit_latest_boundary() -> None:
    api = FakeApi()
    subject = TeamsService(
        FakeAuth(),
        api,  # type: ignore[arg-type]
        read_state_settings("chat:19:direct"),
    )
    result = await subject.mark_chat_read(
        "19:direct",
        through_latest=True,
        confirm=True,
    )
    assert result["status"] == "marked_read"
    assert result["through_message_id"] == "2"
    assert result["write_performed"] is True
    assert api.read_state_writes[0][0:2] == ("19:direct", "2")


@pytest.mark.asyncio
async def test_does_not_move_chat_read_horizon_backwards() -> None:
    api = FakeApi()
    subject = TeamsService(
        FakeAuth(),
        api,  # type: ignore[arg-type]
        read_state_settings("chat:19:direct"),
    )
    result = await subject.mark_chat_read(
        "19:direct",
        through_message_id="1",
        confirm=True,
    )
    assert result["status"] == "already_read"
    assert result["write_performed"] is False
    assert api.read_state_writes == []


@pytest.mark.asyncio
async def test_marks_channel_read_with_separate_allowlist_target() -> None:
    api = FakeApi()
    subject = TeamsService(
        FakeAuth(),
        api,  # type: ignore[arg-type]
        read_state_settings("channel:team-1/channel-1"),
    )
    result = await subject.mark_channel_read(
        "team-1",
        "channel-1",
        through_message_id="2",
        confirm=True,
    )
    assert result["status"] == "marked_read"
    assert api.read_state_writes[0][0:2] == ("channel-1", "2")


@pytest.mark.asyncio
async def test_read_state_write_requires_exactly_one_boundary() -> None:
    api = FakeApi()
    subject = TeamsService(
        FakeAuth(),
        api,  # type: ignore[arg-type]
        read_state_settings("chat:19:direct"),
    )
    with pytest.raises(TeamsMcpError, match="exactly one"):
        await subject.mark_chat_read("19:direct", confirm=True)
    with pytest.raises(TeamsMcpError, match="exactly one"):
        await subject.mark_chat_read(
            "19:direct",
            through_message_id="2",
            through_latest=True,
            confirm=True,
        )
    assert api.read_state_writes == []


@pytest.mark.asyncio
async def test_builds_channel_thread_without_duplicate_reply_roots() -> None:
    result = await service().read_channel_threads("team-1", "channel-1")
    assert result["thread_count"] == 1
    thread = result["threads"][0]  # type: ignore[index]
    assert thread["root"]["id"] == "root-1"
    assert [reply["id"] for reply in thread["replies"]] == ["reply-1"]


@pytest.mark.asyncio
async def test_reads_transcript_and_keeps_ai_recap_separate() -> None:
    result = await service().read_meeting_transcript("19:meeting_abc@thread.v2")
    assert result["status"] == "available"
    assert result["entries"] == [
        {
            "speaker": "Alice",
            "start_time": "00:00:01.000",
            "end_time": "00:00:02.000",
            "text": "Hello",
        }
    ]
    assert result["ai_recap"]["status"] == "not_implemented"  # type: ignore[index]
