from __future__ import annotations

from teams_mcp.parsers import (
    classify_chat,
    html_to_text,
    parse_chat,
    parse_message,
    parse_vtt,
    recording_artifact,
)


def test_html_to_text_keeps_message_text_without_script() -> None:
    source = "<div>Hello &amp; goodbye<br><b>team</b></div><script>secretUiState()</script>"
    assert html_to_text(source) == "Hello & goodbye\nteam"


def test_classifies_chat_types_and_excludes_channels() -> None:
    assert classify_chat({"id": "19:a", "threadProperties": {"memberCount": "2"}}) == "direct"
    assert (
        classify_chat({"id": "19:b", "threadProperties": {"topic": "Project", "memberCount": "4"}})
        == "group"
    )
    assert classify_chat({"id": "19:meeting_abc@thread.v2", "threadProperties": {}}) == "meeting"
    assert classify_chat({"id": "19:c", "threadProperties": {"groupId": "team"}}) is None


def test_parse_chat_reads_preview_only_from_last_message() -> None:
    parsed = parse_chat(
        {
            "id": "19:a",
            "threadProperties": {"memberCount": "2"},
            "properties": {"displayName": "Alice"},
            "lastMessage": {"content": "<p>Latest</p>", "composetime": "2026-08-04T01:00:00Z"},
        }
    )
    assert parsed is not None
    assert parsed.display_name == "Alice"
    assert parsed.last_message_preview == "Latest"


def test_parse_message_handles_json_properties_and_thread_link() -> None:
    parsed = parse_message(
        {
            "id": "reply;part",
            "messagetype": "RichText/Html",
            "content": "<p>Reply &amp; detail</p>",
            "from": "8:orgid:user",
            "imdisplayname": "User",
            "conversationLink": "https://example/19:channel;messageid=root;with;semicolons",
            "properties": '{"subject":"Subject"}',
        },
        "19:channel",
    )
    assert parsed.root_message_id == "root;with;semicolons"
    assert parsed.parent_message_id == "root;with;semicolons"
    assert parsed.content_text == "Reply & detail"
    assert parsed.subject == "Subject"


def test_parse_vtt_handles_multiline_and_unknown_speaker() -> None:
    value = """WEBVTT

00:00:01.000 --> 00:00:03.000
<v Alice>First &amp; second
line</v>

00:03.500 --> 00:05.000
No speaker
"""
    entries = parse_vtt(value)
    assert [entry.speaker for entry in entries] == ["Alice", "Unknown"]
    assert entries[0].text == "First & second\nline"
    assert entries[1].start_time == "00:03.500"


def test_extracts_successful_recording_artifact_without_exposing_other_xml() -> None:
    xml = """
      <RecordingStatus status="Success" />
      <OriginalName v="Weekly &amp; Review" />
      <item uri="https://api.asm.skype.com/v1/transcript?id=signed" type="amsTranscript">
    """
    artifact = recording_artifact(xml)
    assert artifact == {
        "transcript_url": "https://api.asm.skype.com/v1/transcript?id=signed",
        "meeting_title": "Weekly & Review",
    }
    assert recording_artifact(xml.replace("Success", "Failed")) is None
