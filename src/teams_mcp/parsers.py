from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import unquote

from .file_markup import generic_file_labels
from .models import Chat, ChatKind, Message, TranscriptEntry


class _TextExtractor(HTMLParser):
    _break_tags: ClassVar[set[str]] = {
        "br",
        "p",
        "div",
        "li",
        "tr",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "h4",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif tag in self._break_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in self._break_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        text = "".join(parser.parts)
    except Exception:  # malformed Teams HTML must not break an entire result page
        text = re.sub(r"<[^>]+>", " ", value)
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _record(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _properties(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _string(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def classify_chat(raw: dict[str, Any]) -> ChatKind | None:
    conversation_id = str(raw.get("id") or "")
    props = _record(raw.get("threadProperties") or raw.get("threadproperties"))
    if props.get("groupId") or props.get("groupid"):
        return None
    thread_type = str(props.get("threadType") or props.get("threadtype") or "chat").lower()
    if conversation_id == "48:notes" or thread_type == "streamofnotes":
        return "self"
    meeting_markers = (
        "meeting_" in conversation_id.lower()
        or "meeting" in thread_type
        or any(key.lower().startswith("meeting") for key in props)
    )
    if meeting_markers:
        return "meeting"
    if thread_type not in {"", "chat", "thread.v2"}:
        return None
    topic = _string(props, "topic", "spaceThreadTopic")
    member_count = _string(props, "memberCount", "membercount")
    if topic:
        return "group"
    if member_count:
        try:
            return "direct" if int(member_count) <= 2 else "group"
        except ValueError:
            pass
    return "direct"


def parse_chat(raw: dict[str, Any]) -> Chat | None:
    kind = classify_chat(raw)
    if kind is None:
        return None
    props = _record(raw.get("threadProperties") or raw.get("threadproperties"))
    values = _record(raw.get("properties"))
    last = _record(raw.get("lastMessage") or raw.get("lastmessage"))
    count_raw = _string(props, "memberCount", "membercount")
    try:
        member_count = int(count_raw) if count_raw is not None else None
    except ValueError:
        member_count = None
    topic = _string(props, "topic", "spaceThreadTopic")
    display_name = _string(values, "displayName", "displayname") or topic
    timestamp = _string(last, "composetime", "originalarrivaltime") or _string(
        values, "lastimreceivedtime", "lastMessageTime"
    )
    return Chat(
        id=str(raw.get("id") or ""),
        kind=kind,
        topic=topic,
        display_name=display_name,
        member_count=member_count,
        last_message_at=timestamp,
        last_message_preview=html_to_text(_string(last, "content")) or None,
    )


_MESSAGE_ID_RE = re.compile(r";messageid=(.+)$", re.IGNORECASE)


def parse_message(raw: dict[str, Any], conversation_id: str) -> Message:
    props = _properties(raw.get("properties"))
    message_id = _string(raw, "id", "messageId", "messageid") or ""
    root = _string(raw, "rootMessageId", "rootmessageid")
    parent = _string(raw, "parentMessageId", "parentmessageid")
    if not root:
        link = _string(raw, "conversationLink", "conversationlink") or ""
        match = _MESSAGE_ID_RE.search(link)
        if match:
            root = unquote(match.group(1))
    if root == message_id:
        root = None
    if parent == message_id:
        parent = None
    message_type = _string(raw, "messagetype", "messageType", "type") or ""
    deleted = message_type == "MessageDelete" or bool(props.get("deletetime"))
    content = "" if deleted else html_to_text(_string(raw, "content"))
    attachments = (
        generic_file_labels(_string(raw, "content") or "")
        if not deleted and message_type == "RichText/Media_GenericFile"
        else []
    )
    return Message(
        id=message_id,
        conversation_id=conversation_id,
        message_type=message_type,
        sender_id=_string(raw, "from", "senderId", "senderid"),
        sender_display_name=_string(raw, "imdisplayname", "senderDisplayName", "displayName"),
        content_text=content,
        compose_time=_string(raw, "composetime", "composeTime", "createdDateTime"),
        original_arrival_time=_string(
            raw, "originalarrivaltime", "originalArrivalTime", "timestamp"
        ),
        root_message_id=root,
        parent_message_id=parent or root,
        subject=_string(props, "subject") or _string(raw, "subject"),
        is_deleted=deleted,
        attachments=attachments,
    )


_TIMESTAMP_RE = re.compile(
    r"^((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})"
)
_VOICE_RE = re.compile(r"<v(?:\.[^ ]+)?\s+([^>]+)>(.*?)</v>", re.IGNORECASE | re.DOTALL)


def parse_vtt(value: str) -> list[TranscriptEntry]:
    entries: list[TranscriptEntry] = []
    lines = value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = ""
    end = ""
    cue_lines: list[str] = []

    def flush() -> None:
        nonlocal start, end, cue_lines
        if not start or not cue_lines:
            start = end = ""
            cue_lines = []
            return
        cue = "\n".join(cue_lines).strip()
        matches = list(_VOICE_RE.finditer(cue))
        if matches:
            for match in matches:
                text = html_to_text(match.group(2))
                if text:
                    entries.append(
                        TranscriptEntry(
                            speaker=html.unescape(match.group(1)).strip(),
                            start_time=start.replace(",", "."),
                            end_time=end.replace(",", "."),
                            text=text,
                        )
                    )
        else:
            text = html_to_text(cue)
            if text:
                entries.append(
                    TranscriptEntry(
                        speaker="Unknown",
                        start_time=start.replace(",", "."),
                        end_time=end.replace(",", "."),
                        text=text,
                    )
                )
        start = end = ""
        cue_lines = []

    for line in [*lines, ""]:
        timestamp = _TIMESTAMP_RE.match(line)
        if timestamp:
            flush()
            start, end = timestamp.group(1), timestamp.group(2)
        elif not line.strip():
            flush()
        elif start:
            cue_lines.append(line)
    return entries


def recording_artifact(content: str) -> dict[str, str] | None:
    if not re.search(r"<RecordingStatus\b[^>]*\bstatus=['\"]Success['\"]", content, re.I):
        return None
    item = re.search(
        r"<item\b(?=[^>]*\btype=['\"]amsTranscript['\"])(?=[^>]*\buri=['\"]([^'\"]+)['\"])[^>]*>",
        content,
        re.I,
    )
    if not item:
        return None
    title = re.search(r"<OriginalName\b[^>]*\bv=['\"]([^'\"]*)['\"]", content, re.I)
    return {
        "transcript_url": html.unescape(item.group(1)),
        "meeting_title": html.unescape(title.group(1)) if title else "Unknown meeting",
    }
