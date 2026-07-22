from __future__ import annotations

import re

from ..models import MeetingRecap, RecapItem

ROLE_PREFIX_RE = re.compile(
    r'^\s*-?\s*(?P<role>paragraph|text|heading|listitem|list)'
    r'(?:\s+"(?P<quoted>.*)")?\s*:?',
    re.IGNORECASE,
)
REF_RE = re.compile(r"\s*\[ref=[^\]]+\]")
STATE_RE = re.compile(r"\s*\[(?:selected|pressed|expanded|disabled)\]")
INTERACTIVE_RE = re.compile(r"^\s*-?\s*(?:button|link|tab|textbox|menuitem)\b", re.IGNORECASE)

SECTION_MARKERS = (
    "ai recap",
    "meeting recap",
    "intelligent recap",
    "会議のまとめ",
    "会議リキャップ",
    "ai リキャップ",
)
NO_RECAP_MARKERS = (
    "recap isn't available",
    "recap is not available",
    "no recap",
    "リキャップはありません",
    "リキャップを利用できません",
)
NO_ACCESS_MARKERS = (
    "don't have access",
    "do not have access",
    "request access",
    "アクセス権がありません",
    "アクセス許可が必要",
)
IGNORED_LABELS = {
    "chat",
    "teams",
    "calendar",
    "activity",
    "apps",
    "copilot",
    "close",
    "back",
    "more options",
    "チャット",
    "チーム",
    "カレンダー",
    "閉じる",
    "戻る",
    "その他のオプション",
}


def _clean_line(line: str) -> tuple[int, str]:
    indentation = len(line) - len(line.lstrip(" "))
    if INTERACTIVE_RE.match(line):
        return indentation, ""
    text = line.strip()
    text = re.sub(r"^-\s*", "", text)
    role = ROLE_PREFIX_RE.match(text)
    if role:
        quoted = role.group("quoted")
        text = quoted if quoted is not None else text[role.end() :]
    text = REF_RE.sub("", text)
    text = STATE_RE.sub("", text)
    text = text.strip(' :-"')
    if role and role.group("role").casefold() in {"list", "listitem"} and not text:
        return indentation, ""
    return indentation, text


def parse_recap(
    snapshot: str,
    *,
    title: str,
    time_text: str | None = None,
) -> MeetingRecap:
    lowered = snapshot.casefold()
    if any(marker in lowered for marker in NO_ACCESS_MARKERS):
        return MeetingRecap(title=title, time_text=time_text, status="no_access")
    if any(marker in lowered for marker in NO_RECAP_MARKERS):
        return MeetingRecap(title=title, time_text=time_text, status="no_recap")

    raw_lines = snapshot.splitlines()
    start = 0
    for index, line in enumerate(raw_lines):
        if any(marker in line.casefold() for marker in SECTION_MARKERS):
            start = index + 1
            break

    cleaned: list[tuple[int, str]] = []
    seen: set[str] = set()
    for line in raw_lines[start:]:
        indentation, text = _clean_line(line)
        if not text or text.casefold() in IGNORED_LABELS:
            continue
        if any(marker == text.casefold() for marker in SECTION_MARKERS):
            continue
        if text in seen:
            continue
        seen.add(text)
        cleaned.append((indentation, text))

    if not cleaned:
        return MeetingRecap(title=title, time_text=time_text, status="empty")

    base_indent = min(indent for indent, _ in cleaned)
    notes = [
        RecapItem(text=text, level=max(0, (indent - base_indent) // 2))
        for indent, text in cleaned
    ]
    return MeetingRecap(title=title, time_text=time_text, status="ok", notes=notes)
