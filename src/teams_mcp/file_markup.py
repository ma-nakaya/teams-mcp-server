from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_MAX_NAME = 240


def _safe_name(value: object) -> str | None:
    if (
        isinstance(value, str)
        and value
        and len(value) <= _MAX_NAME
        and not any(ord(char) < 32 for char in value)
    ):
        return value
    return None


class _FileLabels(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.labels: list[dict[str, str | None]] = []
        self.inside_file = False
        self.name: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "uriobject":
            self.inside_file = values.get("type") == "File.1"
            self.name = None
        elif self.inside_file and tag == "originalname":
            self.name = _safe_name(values.get("v"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "uriobject":
            if self.inside_file and self.name:
                self.labels.append({"name": self.name, "type": "file"})
            self.inside_file = False
            self.name = None


def generic_file_labels(content: str) -> list[dict[str, str | None]]:
    """Return attachment names only; never expose URI or provider credentials."""
    parser = _FileLabels()
    parser.feed(content)
    return parser.labels


def _sharepoint_url(value: object) -> tuple[str, str] | None:
    """Return a credential-free SharePoint URL and its storage kind, or None."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower()
        has_port = parsed.port is not None
    except ValueError:  # malformed host or port must not abort a whole message read
        return None
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or has_port
        or not host.endswith(".sharepoint.com")
    ):
        return None
    storage = "onedrive" if host.endswith("-my.sharepoint.com") else "sharepoint"
    # Drop query and fragment: they can carry share tokens or access hints.
    return urlunsplit(("https", host, parsed.path, "", "")), storage


def shared_file_labels(raw_files: object) -> list[dict[str, str | None]]:
    """Return SharePoint/OneDrive attachments from a message's `properties.files`.

    Teams stores ordinary attachments here, not in the message HTML. Only the
    name, extension, storage kind and a query-free SharePoint URL are returned.
    """
    files: Any = raw_files
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except json.JSONDecodeError:
            return []
    if not isinstance(files, list):
        return []
    labels: list[dict[str, str | None]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = _safe_name(item.get("fileName")) or _safe_name(item.get("title"))
        if not name:
            continue
        location = _sharepoint_url(item.get("objectUrl"))
        file_type = item.get("fileType")
        labels.append(
            {
                "name": name,
                "type": "file",
                "file_type": file_type if isinstance(file_type, str) and file_type else None,
                "storage": location[1] if location else None,
                "url": location[0] if location else None,
            }
        )
    return labels
