from __future__ import annotations

from datetime import date
from typing import Any

from .browser import PlaywrightCli
from .errors import TeamsMcpError
from .services.recap_service import RecapService
from .session import TeamsSession


def collect_meeting_recaps(date_text: str) -> dict[str, Any]:
    try:
        target_date = date.fromisoformat(date_text)
    except ValueError:
        return {
            "status": "invalid_request",
            "message": "dateはYYYY-MM-DD形式で指定してください。",
        }

    try:
        browser = PlaywrightCli()
        session = TeamsSession(browser)
        return RecapService(browser, session).collect(target_date)
    except TeamsMcpError as exc:
        return exc.as_result()
    except FileNotFoundError as exc:
        return {"status": "internal_error", "message": str(exc)}

