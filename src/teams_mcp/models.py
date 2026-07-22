from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass(slots=True)
class RecapItem:
    text: str
    level: int = 0


@dataclass(slots=True)
class MeetingRecap:
    title: str
    time_text: str | None
    status: Literal["ok", "no_recap", "no_access", "empty"]
    notes: list[RecapItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

