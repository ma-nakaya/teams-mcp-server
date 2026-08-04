from __future__ import annotations

import logging
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .api import TeamsApi
from .auth import AuthManager
from .config import Settings
from .service import TeamsService

settings = Settings()
auth = AuthManager(settings)
api = TeamsApi(auth, settings)
service = TeamsService(auth, api, settings)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

mcp = FastMCP(
    "Teams (local, no Graph)",
    instructions=(
        "Local Microsoft Teams access through unsupported Teams web APIs. "
        "teams_auth_status refreshes an expired session when possible; only request a new sign-in "
        "when it returns usable=false and reauthentication_required=true. "
        "Never request or expose auth tokens. Writes are disabled by default and require an "
        "allowlisted target plus confirm=true. Reading content never marks it read. Read-state "
        "changes use a separate disabled-by-default gate and an explicit message boundary."
    ),
)


@mcp.tool(annotations=READ_ONLY)
async def teams_auth_status() -> dict[str, object]:
    """期限切れなら安全に自動更新し、利用可能な認証状態だけを返します。"""
    return await service.auth_status()


@mcp.tool(annotations=READ_ONLY)
async def list_recent_chats(
    kind: Literal["all", "direct", "group", "meeting", "self"] = "all",
    limit: int = 50,
    unread_only: bool = False,
) -> dict[str, object]:
    """既読状態つきでDM、グループ、会議チャットを一覧化します。"""
    return await service.list_recent_chats(
        kind=kind,
        limit=limit,
        unread_only=unread_only,
    )


@mcp.tool(annotations=READ_ONLY)
async def read_chat(
    chat_id: str,
    limit: int = 100,
    before_ms: int | None = None,
) -> dict[str, object]:
    """DMまたはグループチャットのメッセージ本文を読みます。"""
    return await service.read_chat(chat_id, limit=limit, before_ms=before_ms)


@mcp.tool(annotations=READ_ONLY)
async def list_teams() -> dict[str, object]:
    """参加中のチームを一覧化します。"""
    return await service.list_teams()


@mcp.tool(annotations=READ_ONLY)
async def list_channels(team_id: str, unread_only: bool = False) -> dict[str, object]:
    """既読状態つきで指定チームのチャネルを一覧化します。"""
    return await service.list_channels(team_id, unread_only=unread_only)


@mcp.tool(annotations=READ_ONLY)
async def read_channel_threads(
    team_id: str,
    channel_id: str,
    thread_limit: int = 25,
    reply_limit: int = 50,
) -> dict[str, object]:
    """チャネルの親投稿と返信をスレッド構造で読みます。"""
    return await service.read_channel_threads(
        team_id,
        channel_id,
        thread_limit=thread_limit,
        reply_limit=reply_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def read_channel_thread(
    team_id: str,
    channel_id: str,
    root_message_id: str,
    reply_limit: int = 100,
) -> dict[str, object]:
    """指定したチャネル親投稿への返信を読みます。"""
    return await service.read_channel_thread(
        team_id,
        channel_id,
        root_message_id,
        reply_limit=reply_limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_meeting_chats(
    limit: int = 50,
    unread_only: bool = False,
) -> dict[str, object]:
    """既読状態つきで会議チャットを一覧化します。"""
    return await service.list_meeting_chats(limit=limit, unread_only=unread_only)


@mcp.tool(annotations=READ_ONLY)
async def read_meeting_chat(
    chat_id: str,
    limit: int = 200,
    before_ms: int | None = None,
) -> dict[str, object]:
    """会議チャットのメッセージを読みます。"""
    return await service.read_meeting_chat(chat_id, limit=limit, before_ms=before_ms)


@mcp.tool(annotations=READ_ONLY)
async def read_meeting_transcript(
    chat_id: str,
    max_entries: int = 2_000,
    include_raw_vtt: bool = False,
) -> dict[str, object]:
    """会議チャット内の録画メタデータからVTT文字起こしを取得します。"""
    return await service.read_meeting_transcript(
        chat_id,
        max_entries=max_entries,
        include_raw_vtt=include_raw_vtt,
    )


@mcp.tool(annotations=READ_ONLY)
async def collect_meeting_recaps(
    limit_chats: int = 10,
    max_entries_per_meeting: int = 500,
) -> dict[str, object]:
    """最近の会議の文字起こしを収集します。AIリキャップとの区別も返します。"""
    return await service.collect_meeting_recaps(
        limit_chats=limit_chats,
        max_entries_per_meeting=max_entries_per_meeting,
    )


@mcp.tool(annotations=READ_ONLY)
async def collect_meeting_artifacts(
    limit_chats: int = 10,
    max_entries_per_meeting: int = 500,
) -> dict[str, object]:
    """collect_meeting_recapsの意味が明確な別名です。"""
    return await service.collect_meeting_recaps(
        limit_chats=limit_chats,
        max_entries_per_meeting=max_entries_per_meeting,
    )


@mcp.tool(annotations=WRITE)
async def mark_chat_read(
    chat_id: str,
    through_message_id: str | None = None,
    through_latest: bool = False,
    confirm: bool = False,
) -> dict[str, object]:
    """許可済みチャットを指定メッセージまで既読にします。自動既読は行いません。"""
    return await service.mark_chat_read(
        chat_id,
        through_message_id=through_message_id,
        through_latest=through_latest,
        confirm=confirm,
    )


@mcp.tool(annotations=WRITE)
async def mark_channel_read(
    team_id: str,
    channel_id: str,
    through_message_id: str | None = None,
    through_latest: bool = False,
    confirm: bool = False,
) -> dict[str, object]:
    """許可済みチャネルを指定メッセージまで既読にします。自動既読は行いません。"""
    return await service.mark_channel_read(
        team_id,
        channel_id,
        through_message_id=through_message_id,
        through_latest=through_latest,
        confirm=confirm,
    )


@mcp.tool(annotations=WRITE)
async def send_chat_message(
    chat_id: str,
    content: str,
    confirm: bool = False,
) -> dict[str, object]:
    """許可済みDM/チャットへ送信します。confirm=trueが必須です。"""
    return await service.send_chat_message(chat_id, content, confirm=confirm)


@mcp.tool(annotations=WRITE)
async def send_channel_post(
    team_id: str,
    channel_id: str,
    content: str,
    confirm: bool = False,
) -> dict[str, object]:
    """許可済みチャネルへ親投稿を送信します。confirm=trueが必須です。"""
    return await service.send_channel_post(team_id, channel_id, content, confirm=confirm)


@mcp.tool(annotations=WRITE)
async def reply_channel_thread(
    team_id: str,
    channel_id: str,
    root_message_id: str,
    content: str,
    confirm: bool = False,
) -> dict[str, object]:
    """許可済みチャネルスレッドへ返信します。confirm=trueが必須です。"""
    return await service.reply_channel_thread(
        team_id,
        channel_id,
        root_message_id,
        content,
        confirm=confirm,
    )


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings.validate()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
