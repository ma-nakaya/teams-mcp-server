from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class TeamsMcpError(Exception):
    message: str
    status: str = "internal_error"
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message

    def as_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class AuthenticationRequiredError(TeamsMcpError):
    def __init__(self, message: str = "Teamsへの再ログインが必要です。") -> None:
        super().__init__(message=message, status="authentication_required")


class SessionBusyError(TeamsMcpError):
    def __init__(self) -> None:
        super().__init__(
            message="Teamsセッションは別の取得処理で使用中です。",
            status="busy",
        )


class BrowserTimeoutError(TeamsMcpError):
    def __init__(self, operation: str) -> None:
        super().__init__(
            message=f"Teams UIの応答待ちがタイムアウトしました: {operation}",
            status="timeout",
        )


class UiChangedError(TeamsMcpError):
    def __init__(self, message: str) -> None:
        super().__init__(message=message, status="ui_changed")

