from __future__ import annotations

import pytest

from teams_mcp.server import mcp


@pytest.mark.asyncio
async def test_expected_mcp_tools_are_registered() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {
        "list_recent_chats",
        "read_chat",
        "list_teams",
        "list_channels",
        "read_channel_threads",
        "read_channel_thread",
        "list_meeting_chats",
        "read_meeting_chat",
        "read_meeting_transcript",
        "collect_meeting_recaps",
        "send_chat_message",
        "send_channel_post",
        "reply_channel_thread",
    } <= names
    by_name = {tool.name: tool for tool in tools}
    assert by_name["read_chat"].annotations is not None
    assert by_name["read_chat"].annotations.readOnlyHint is True
    assert by_name["send_chat_message"].annotations is not None
    assert by_name["send_chat_message"].annotations.destructiveHint is True
