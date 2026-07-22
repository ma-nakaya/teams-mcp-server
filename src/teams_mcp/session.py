from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from .browser import PlaywrightCli
from .errors import AuthenticationRequiredError, SessionBusyError

TEAMS_URL = "https://teams.microsoft.com/v2/"


def default_lock_path() -> Path:
    configured = os.environ.get("TEAMS_MCP_LOCK_PATH")
    if configured:
        return Path(configured).expanduser()
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "teams-mcp" / "teams-session.lock"
    return Path(tempfile.gettempdir()) / "teams-mcp" / "teams-session.lock"


class TeamsSession:
    def __init__(
        self,
        browser: PlaywrightCli,
        *,
        lock_path: Path | None = None,
        lock_timeout_seconds: float = 5,
    ) -> None:
        self.browser = browser
        self.lock_path = lock_path or default_lock_path()
        self.lock_timeout_seconds = lock_timeout_seconds

    @contextmanager
    def acquire(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            self._lock(handle)
            yield
        finally:
            self._unlock(handle)
            handle.close()

    def _lock(self, handle: BinaryIO) -> None:
        deadline = time.monotonic() + self.lock_timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    if self.lock_path.stat().st_size == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise SessionBusyError() from exc
                time.sleep(0.1)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        if handle.closed:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def normalize(self) -> str:
        self.browser.goto(TEAMS_URL)
        snapshot = self.browser.snapshot()
        self.ensure_authenticated(snapshot)
        return snapshot

    @staticmethod
    def ensure_authenticated(snapshot: str) -> None:
        lowered = snapshot.casefold()
        login_markers = (
            "sign in",
            "pick an account",
            "enter password",
            "use another account",
            "サインイン",
            "アカウントを選択",
            "パスワードの入力",
        )
        if any(marker in lowered for marker in login_markers):
            raise AuthenticationRequiredError()
