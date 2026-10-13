from __future__ import annotations

from html.parser import HTMLParser


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
            value = values.get("v")
            if value and len(value) <= 240 and not any(ord(char) < 32 for char in value):
                self.name = value

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
