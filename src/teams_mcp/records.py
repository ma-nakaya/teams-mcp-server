"""Defensive accessors for loosely typed Teams JSON records."""

from __future__ import annotations

from typing import Any


def as_record(value: object) -> dict[str, Any]:
    """Return the value if it is a JSON object, otherwise an empty one."""
    return value if isinstance(value, dict) else {}


def first_string(record: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty value among keys, as a string."""
    for key in keys:
        value = record.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def record_rows(value: object, *keys: str) -> list[dict[str, Any]]:
    """Return JSON objects from a list, or from the first list found under keys."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in keys:
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []
