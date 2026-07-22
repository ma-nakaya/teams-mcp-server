from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .application import collect_meeting_recaps


def _markdown(result: dict[str, Any]) -> str:
    if result.get("status") != "ok":
        return f"# Teams meeting recaps\n\nError: {result.get('message', 'unknown error')}\n"

    lines = [f"# Teams meeting recaps — {result['date']}", ""]
    meetings = result.get("meetings", [])
    if not meetings:
        lines.append("No matching meeting recaps were found.")
        return "\n".join(lines) + "\n"
    for meeting in meetings:
        title = meeting["title"]
        if meeting.get("time_text"):
            title += f" ({meeting['time_text']})"
        lines.extend([f"## {title}", "", f"Status: `{meeting['status']}`", ""])
        for note in meeting.get("notes", []):
            indent = "  " * int(note.get("level", 0))
            lines.append(f"{indent}- {note['text']}")
        lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Teams meeting AI recaps")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = collect_meeting_recaps(args.date)
    output = (
        json.dumps(result, ensure_ascii=False, indent=2)
        if args.format == "json"
        else _markdown(result)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + ("\n" if args.format == "json" else ""), encoding="utf-8")
    else:
        print(output)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

