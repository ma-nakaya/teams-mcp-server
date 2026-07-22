from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .application import collect_meeting_recaps as collect_recaps

mcp = FastMCP(
    "teams-mcp",
    instructions=(
        "Read-only access to Microsoft Teams Web through the local authenticated "
        "Playwright session. Never use this server to send or modify Teams data."
    ),
)


@mcp.tool()
def collect_meeting_recaps(date: str) -> dict[str, Any]:
    """Collect AI-generated Teams meeting recaps for a date (YYYY-MM-DD)."""
    return collect_recaps(date)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

