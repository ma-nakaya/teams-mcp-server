from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from .api import TeamsApi
from .auth import AuthManager
from .config import Settings
from .errors import TeamsMcpError
from .models import Channel, Message, Team, Thread
from .parsers import parse_chat, parse_message, parse_vtt, recording_artifact
from .write_guard import WriteGuard

ChatFilter = Literal["all", "direct", "group", "meeting", "self"]


def _rows(value: object, *keys: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in keys:
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _raw_string(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value):
            return str(value)
    return None


class TeamsService:
    def __init__(
        self,
        auth: AuthManager,
        api: TeamsApi,
        settings: Settings | None = None,
    ) -> None:
        self.auth = auth
        self.api = api
        self.settings = settings or auth.settings
        self.writes = WriteGuard(self.settings)

    def auth_status(self) -> dict[str, object]:
        return self.auth.status()

    async def list_recent_chats(
        self, *, kind: ChatFilter = "all", limit: int = 50
    ) -> dict[str, object]:
        if kind not in {"all", "direct", "group", "meeting", "self"}:
            raise ValueError("kind must be all, direct, group, meeting, or self")
        limit = min(max(limit, 1), 200)
        chats = [chat for raw in await self.api.list_conversations() if (chat := parse_chat(raw))]
        if kind != "all":
            chats = [chat for chat in chats if chat.kind == kind]
        chats.sort(key=lambda item: item.last_message_at or "", reverse=True)
        selected = chats[:limit]
        return {"count": len(selected), "chats": [chat.to_dict() for chat in selected]}

    async def read_chat(
        self,
        chat_id: str,
        *,
        limit: int = 100,
        before_ms: int | None = None,
        max_pages: int = 5,
    ) -> dict[str, object]:
        limit = min(max(limit, 1), 500)
        messages: list[Message] = []
        seen: set[str] = set()
        async for page in self.api.message_pages(
            chat_id,
            page_size=min(limit, 200),
            max_pages=max_pages,
            before_ms=before_ms,
        ):
            for raw in page:
                message = parse_message(raw, chat_id)
                key = message.id or json.dumps(raw, sort_keys=True, default=str)
                if key in seen:
                    continue
                seen.add(key)
                messages.append(message)
                if len(messages) >= limit:
                    break
            if len(messages) >= limit:
                break
        messages.sort(key=lambda item: item.compose_time or item.original_arrival_time or "")
        return {
            "chat_id": chat_id,
            "count": len(messages),
            "messages": [message.to_dict() for message in messages],
        }

    async def list_teams(self) -> dict[str, object]:
        teams: list[Team] = []
        snapshot = await self.api.teams_snapshot()
        for raw in _rows(snapshot, "teams"):
            team_id = _raw_string(raw, "id", "teamId")
            if not team_id:
                continue
            if raw.get("isDeleted") is True:
                continue
            teams.append(
                Team(
                    id=team_id,
                    name=_raw_string(raw, "displayName", "name") or "Unknown team",
                )
            )
        teams.sort(key=lambda team: team.name.casefold())
        return {"count": len(teams), "teams": [team.to_dict() for team in teams]}

    async def list_channels(self, team_id: str) -> dict[str, object]:
        snapshot = await self.api.teams_snapshot()
        selected = next(
            (
                team
                for team in _rows(snapshot, "teams")
                if _raw_string(team, "id", "teamId") == team_id
            ),
            None,
        )
        if selected is None:
            return {"team_id": team_id, "count": 0, "channels": []}
        channels: list[Channel] = []
        for item in _rows(selected, "channels"):
            if item.get("isDeleted") is True:
                continue
            channel_id = _raw_string(item, "id", "channelId", "threadId")
            if not channel_id:
                continue
            channels.append(
                Channel(
                    id=channel_id,
                    team_id=team_id,
                    name=_raw_string(item, "name", "displayName", "topic") or "Unknown channel",
                    description=_raw_string(item, "description"),
                )
            )
        return {
            "team_id": team_id,
            "count": len(channels),
            "channels": [c.to_dict() for c in channels],
        }

    async def read_channel_thread(
        self,
        team_id: str,
        channel_id: str,
        root_message_id: str,
        *,
        reply_limit: int = 100,
    ) -> dict[str, object]:
        raw = await self.api.channel_replies(
            team_id, channel_id, root_message_id, limit=min(max(reply_limit, 1), 100)
        )
        replies = [
            parse_message(item, channel_id)
            for item in _rows(raw, "messages", "replies", "value")
            if _raw_string(item, "id", "messageId") != root_message_id
        ]
        replies.sort(key=lambda item: item.compose_time or item.original_arrival_time or "")
        return {
            "team_id": team_id,
            "channel_id": channel_id,
            "root_message_id": root_message_id,
            "reply_count": len(replies),
            "replies": [message.to_dict() for message in replies],
        }

    async def read_channel_threads(
        self,
        team_id: str,
        channel_id: str,
        *,
        thread_limit: int = 25,
        reply_limit: int = 50,
    ) -> dict[str, object]:
        thread_limit = min(max(thread_limit, 1), 50)
        raw = await self.api.channel_messages(team_id, channel_id, limit=thread_limit)
        parsed = [parse_message(item, channel_id) for item in _rows(raw, "messages", "value")]
        roots = [
            message
            for message in parsed
            if message.root_message_id is None and message.parent_message_id is None
        ][:thread_limit]
        semaphore = asyncio.Semaphore(4)

        async def load(root: Message) -> Thread:
            async with semaphore:
                raw_replies = await self.api.channel_replies(
                    team_id, channel_id, root.id, limit=min(max(reply_limit, 1), 100)
                )
            replies = [
                parse_message(item, channel_id)
                for item in _rows(raw_replies, "messages", "replies", "value")
                if _raw_string(item, "id", "messageId") != root.id
            ]
            replies.sort(key=lambda item: item.compose_time or item.original_arrival_time or "")
            return Thread(root=root, replies=replies)

        threads = await asyncio.gather(*(load(root) for root in roots))
        threads.sort(
            key=lambda item: item.root.compose_time or item.root.original_arrival_time or ""
        )
        return {
            "team_id": team_id,
            "channel_id": channel_id,
            "thread_count": len(threads),
            "threads": [thread.to_dict() for thread in threads],
        }

    async def list_meeting_chats(self, *, limit: int = 50) -> dict[str, object]:
        return await self.list_recent_chats(kind="meeting", limit=limit)

    async def read_meeting_chat(
        self, chat_id: str, *, limit: int = 200, before_ms: int | None = None
    ) -> dict[str, object]:
        return await self.read_chat(chat_id, limit=limit, before_ms=before_ms, max_pages=10)

    async def read_meeting_transcript(
        self,
        chat_id: str,
        *,
        max_pages: int = 20,
        max_entries: int = 2_000,
        include_raw_vtt: bool = False,
    ) -> dict[str, object]:
        max_entries = min(max(max_entries, 1), 10_000)
        artifact: dict[str, str] | None = None
        source_message_id: str | None = None
        async for page in self.api.message_pages(
            chat_id, page_size=200, max_pages=min(max(max_pages, 1), 20)
        ):
            for raw in page:
                message_type = _raw_string(raw, "messagetype", "messageType")
                if message_type != "RichText/Media_CallRecording":
                    continue
                content = _raw_string(raw, "content") or ""
                artifact = recording_artifact(content)
                if artifact:
                    source_message_id = _raw_string(raw, "id", "messageId")
                    break
            if artifact:
                break
        if artifact is None:
            return {
                "chat_id": chat_id,
                "status": "not_found",
                "reason": (
                    "No successful meeting-recording message with an AMS transcript was found."
                ),
                "entries": [],
            }
        raw_vtt = await self.api.transcript_vtt(artifact["transcript_url"])
        entries = parse_vtt(raw_vtt)
        truncated = len(entries) > max_entries
        entries = entries[:max_entries]
        result: dict[str, object] = {
            "chat_id": chat_id,
            "status": "available",
            "meeting_title": artifact["meeting_title"],
            "source_message_id": source_message_id,
            "entry_count": len(entries),
            "truncated": truncated,
            "entries": [entry.to_dict() for entry in entries],
            "ai_recap": {
                "status": "not_implemented",
                "reason": (
                    "AI notes/recap are separate from the transcript and no stable internal "
                    "endpoint has been validated."
                ),
            },
        }
        if include_raw_vtt:
            result["raw_vtt"] = raw_vtt
        return result

    async def collect_meeting_recaps(
        self, *, limit_chats: int = 10, max_entries_per_meeting: int = 500
    ) -> dict[str, object]:
        chats_result = await self.list_meeting_chats(limit=min(max(limit_chats, 1), 25))
        chats = chats_result["chats"]
        if not isinstance(chats, list):
            chats = []
        semaphore = asyncio.Semaphore(3)

        async def collect(chat: dict[str, Any]) -> dict[str, object]:
            async with semaphore:
                try:
                    result = await self.read_meeting_transcript(
                        str(chat["id"]), max_entries=max_entries_per_meeting
                    )
                except TeamsMcpError as exc:
                    result = {"chat_id": str(chat["id"]), "status": "error", "error": str(exc)}
            result["chat"] = chat
            return result

        artifacts = await asyncio.gather(
            *(collect(chat) for chat in chats if isinstance(chat, dict) and chat.get("id"))
        )
        return {
            "count": len(artifacts),
            "meetings": artifacts,
            "note": (
                "The tool returns meeting transcripts. Teams AI recap/notes are reported "
                "separately and are not synthesized or mislabeled."
            ),
        }

    async def send_chat_message(
        self, chat_id: str, content: str, *, confirm: bool
    ) -> dict[str, object]:
        authorized = self.writes.authorize(
            target=f"chat:{chat_id}", content=content, confirm=confirm
        )
        response = await self.api.post_chat_message(chat_id, authorized.content)
        return {
            "status": "sent",
            "target": authorized.target,
            "content_sha256": authorized.content_sha256,
            "response": response,
        }

    async def send_channel_post(
        self, team_id: str, channel_id: str, content: str, *, confirm: bool
    ) -> dict[str, object]:
        target = f"channel:{team_id}/{channel_id}"
        authorized = self.writes.authorize(target=target, content=content, confirm=confirm)
        response = await self.api.post_channel_message(team_id, channel_id, authorized.content)
        return {
            "status": "sent",
            "target": target,
            "content_sha256": authorized.content_sha256,
            "response": response,
        }

    async def reply_channel_thread(
        self,
        team_id: str,
        channel_id: str,
        root_message_id: str,
        content: str,
        *,
        confirm: bool,
    ) -> dict[str, object]:
        target = f"channel:{team_id}/{channel_id}"
        authorized = self.writes.authorize(target=target, content=content, confirm=confirm)
        response = await self.api.post_channel_message(
            team_id,
            channel_id,
            authorized.content,
            root_message_id=root_message_id,
        )
        return {
            "status": "sent",
            "target": target,
            "root_message_id": root_message_id,
            "content_sha256": authorized.content_sha256,
            "response": response,
        }
