from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import BrowserTimeoutError, TeamsMcpError


def find_playwright_cli() -> str:
    configured = os.environ.get("PLAYWRIGHT_CLI")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        raise FileNotFoundError(f"PLAYWRIGHT_CLI does not exist: {path}")

    executable = shutil.which("playwright-cli")
    if executable:
        return executable
    raise FileNotFoundError(
        "playwright-cli not found. Install it with: npm install -g @playwright/cli"
    )


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class PlaywrightCli:
    def __init__(
        self,
        *,
        executable: str | None = None,
        session: str = "teams",
        timeout_seconds: float = 30,
    ) -> None:
        self.executable = executable or find_playwright_cli()
        self.session = session
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        *arguments: str,
        timeout_seconds: float | None = None,
        check: bool = True,
    ) -> CommandResult:
        command: Sequence[str] = [self.executable, f"-s={self.session}", *arguments]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds or self.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrowserTimeoutError(" ".join(arguments)) from exc

        result = CommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
        if check and result.returncode != 0:
            raise TeamsMcpError(
                message="Playwright CLI command failed.",
                status="internal_error",
                details={
                    "operation": arguments[0] if arguments else "unknown",
                    "returnCode": result.returncode,
                    "stderr": result.stderr[-1000:],
                },
            )
        return result

    def snapshot(self) -> str:
        return self.run("snapshot").stdout

    def goto(self, url: str) -> None:
        self.run("goto", url, timeout_seconds=60)

    def click(self, ref: str) -> None:
        self.run("click", ref)

    def open(self, url: str, *, headed: bool = False) -> None:
        arguments = ["open", url]
        if headed:
            arguments.append("--headed")
        self.run(*arguments, timeout_seconds=120)

