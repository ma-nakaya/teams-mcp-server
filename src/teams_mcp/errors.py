from __future__ import annotations


class TeamsMcpError(RuntimeError):
    """Base error with a stable, non-secret error code."""

    def __init__(self, message: str, code: str = "teams_error") -> None:
        super().__init__(message)
        self.code = code


class AuthenticationError(TeamsMcpError):
    pass


class ApiError(TeamsMcpError):
    def __init__(
        self,
        message: str,
        code: str = "api_error",
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code)
        self.status_code = status_code
        self.request_id = request_id


class WriteRejectedError(TeamsMcpError):
    pass
