from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from .api import TeamsApi
from .auth import AuthManager, DeviceCodePrompt, PendingDeviceLogin
from .config import Settings
from .credentials import KeyringPendingLoginStore
from .errors import TeamsMcpError
from .service import TeamsService


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def _display_name(properties: dict[str, Any]) -> str | None:
    details = properties.get("userDetails")
    if isinstance(details, str):
        try:
            parsed = json.loads(details)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("name"), str):
            return str(parsed["name"])
    for key in ("displayName", "primaryMemberName"):
        value = properties.get(key)
        if isinstance(value, str) and value:
            return value
    return None


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    settings.validate()
    auth = AuthManager(settings)
    api = TeamsApi(auth, settings)
    service = TeamsService(auth, api, settings)
    pending_store = KeyringPendingLoginStore(settings.credential_profile)
    try:
        if args.command == "status":
            _print_json(await auth.ensure_status())
            return 0
        if args.command == "logout":
            auth.logout()
            _print_json({"authenticated": False, "profile": settings.credential_profile})
            return 0
        if args.command == "login":

            def show_code(prompt: DeviceCodePrompt) -> None:
                print(f"Open: {prompt.verification_uri}", flush=True)
                print(f"Code: {prompt.user_code}", flush=True)
                print("Waiting for Microsoft sign-in approval...", flush=True)

            bundle = await auth.login(open_browser=not args.no_browser, on_code=show_code)
            properties = await api.user_properties()
            display_name = _display_name(properties)
            if display_name:
                bundle.display_name = display_name
                auth.store.save(bundle)
            _print_json(
                {
                    "authenticated": True,
                    "profile": settings.credential_profile,
                    "tenant_id": bundle.tenant_id,
                    "region": bundle.region,
                    "display_name": bundle.display_name,
                }
            )
            return 0
        if args.command == "login-start":
            pending = await auth.begin_login(open_browser=not args.no_browser)
            pending_store.save(pending.to_secret_dict())
            _print_json(
                {
                    "status": "pending",
                    "verification_uri": pending.prompt.verification_uri,
                    "verification_uri_complete": pending.prompt.verification_uri_complete,
                    "user_code": pending.prompt.user_code,
                    "expires_at": pending.expires_at,
                }
            )
            return 0
        if args.command == "login-complete":
            raw_pending = pending_store.load()
            if raw_pending is None:
                raise TeamsMcpError(
                    "No pending login. Run `teams-mcp login-start` first.",
                    "pending_missing",
                )
            pending = PendingDeviceLogin.from_secret_dict(raw_pending)
            bundle = await auth.complete_login(pending)
            pending_store.delete()
            properties = await api.user_properties()
            display_name = _display_name(properties)
            if display_name:
                bundle.display_name = display_name
                auth.store.save(bundle)
            _print_json(
                {
                    "authenticated": True,
                    "profile": settings.credential_profile,
                    "tenant_id": bundle.tenant_id,
                    "region": bundle.region,
                    "display_name": bundle.display_name,
                }
            )
            return 0
        if args.command == "probe":
            properties, chats, teams = await asyncio.gather(
                api.user_properties(),
                service.list_recent_chats(limit=5),
                service.list_teams(),
            )
            _print_json(
                {
                    "authenticated": True,
                    "display_name": _display_name(properties),
                    "recent_chat_count": chats["count"],
                    "team_count": teams["count"],
                    "writes_enabled": settings.writes_enabled,
                    "read_state_writes_enabled": settings.read_state_writes_enabled,
                }
            )
            return 0
        raise ValueError(f"Unknown command: {args.command}")
    finally:
        await api.aclose()
        await auth.aclose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Teams MCP authentication helper")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="Sign in with Microsoft's device-code flow")
    login.add_argument(
        "--no-browser", action="store_true", help="Print the code without opening a browser"
    )
    login_start = sub.add_parser(
        "login-start", help="Start a login and securely save the short-lived device code"
    )
    login_start.add_argument(
        "--no-browser", action="store_true", help="Print the code without opening a browser"
    )
    sub.add_parser("login-complete", help="Complete a previously started device-code login")
    sub.add_parser("status", help="Show non-secret authentication status")
    sub.add_parser("probe", help="Test read access without printing message content")
    sub.add_parser("logout", help="Remove this profile from the OS credential vault")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        raise SystemExit(asyncio.run(_run(_parser().parse_args())))
    except (TeamsMcpError, ValueError) as exc:
        code = getattr(exc, "code", "invalid_configuration")
        _print_json({"ok": False, "error": str(exc), "code": code})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
