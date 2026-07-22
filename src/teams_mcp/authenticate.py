from __future__ import annotations

import argparse
import sys

from .browser import PlaywrightCli
from .errors import TeamsMcpError
from .session import TEAMS_URL, TeamsSession


def main() -> int:
    parser = argparse.ArgumentParser(description="Open Teams and refresh its login session")
    parser.add_argument("--session", default="teams")
    args = parser.parse_args()
    try:
        browser = PlaywrightCli(session=args.session, timeout_seconds=120)
        browser.open(TEAMS_URL, headed=True)
        snapshot = browser.snapshot()
        TeamsSession.ensure_authenticated(snapshot)
    except (TeamsMcpError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Teams authentication session is available.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

