from __future__ import annotations

import re

REF_RE = re.compile(r"\[ref=(?P<ref>[^\]]+)\]")
QUOTED_NAME_RE = re.compile(r'\b(?:button|link|tab|listitem)\s+"(?P<name>[^"]+)"')


def find_ref(snapshot: str, *labels: str) -> str | None:
    wanted = tuple(label.casefold() for label in labels)
    for line in snapshot.splitlines():
        lowered = line.casefold()
        if not any(label in lowered for label in wanted):
            continue
        match = REF_RE.search(line)
        if match:
            return match.group("ref")
    return None


def referenced_lines(snapshot: str) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for line in snapshot.splitlines():
        ref = REF_RE.search(line)
        if not ref:
            continue
        name = QUOTED_NAME_RE.search(line)
        label = name.group("name") if name else line[: ref.start()].strip(" -")
        results.append((ref.group("ref"), label))
    return results

