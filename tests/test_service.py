from __future__ import annotations

from typing import Any

import pytest

from teams_mcp.config import Settings
from teams_mcp.service import TeamsService


class FakeAuth:
    settings = Settings()

    async def ensure_status(self) -> dict[str, object]:
        return {"authenticated": True, "usable": True}


class FakeApi:
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

    async def teams_snapshot(self) -> dict[str, object]:
        return {
            "teams": [
                {
                    "id": "team-1",
                    "displayName": "Team One",
                    "channels": [
                        {
                            "id": "channel-1",
                            "displayName": "General",
                            "description": "General channel",
                        }
                    ],
                }
            ]
        }

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
    assert teams == {"count": 1, "teams": [{"id": "team-1", "name": "Team One"}]}


@pytest.mark.asyncio
async def test_lists_channels_from_csa_snapshot() -> None:
    channels = await service().list_channels("team-1")
    assert channels == {
        "team_id": "team-1",
        "count": 1,
        "channels": [
            {
                "id": "channel-1",
                "team_id": "team-1",
                "name": "General",
                "description": "General channel",
            }
        ],
    }


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
