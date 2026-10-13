from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any
from uuid import UUID

from filelock import FileLock, Timeout
from mcp.types import CallToolResult, TextContent
from platformdirs import user_cache_path

from .api import TeamsApi
from .config import Settings
from .errors import TeamsMcpError, WriteRejectedError
from .file_api import TeamsFileApi, _self_mri
from .write_guard import WriteGuard

MAX_FILE_BYTES = 10 * 1024 * 1024
SELF_CHAT = "48:notes"
logger = logging.getLogger("teams_mcp.audit")


def _pdf_bytes(file_path: str, maximum: int = MAX_FILE_BYTES) -> tuple[str, bytes]:
    path = Path(file_path)
    if (
        not path.is_absolute()
        or str(path).startswith(("\\\\", "//"))
        or ":" in path.name
        or path.suffix.lower() != ".pdf"
        or len(path.name) > 240
        or len(path.name.encode("utf-8", errors="replace")) > 255
        or any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
            for char in path.name
        )
    ):
        raise WriteRejectedError("Use an absolute local PDF path.", "file_path_invalid")
    try:
        for item in (path, *path.parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise WriteRejectedError(
                    "Linked file paths are not supported.", "file_path_invalid"
                )
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                raise WriteRejectedError(
                    "PDF is empty or exceeds the configured size limit.", "file_size_invalid"
                )
            data = stream.read(maximum + 1)
    except OSError as exc:
        raise WriteRejectedError("The local PDF could not be read.", "file_unreadable") from exc
    if len(data) != info.st_size or len(data) > maximum:
        raise WriteRejectedError("The local PDF changed while reading.", "file_changed")
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
        raise WriteRejectedError("The file is not a complete PDF.", "file_format_invalid")
    return path.name, data


async def file_tool_result(operation: Awaitable[dict[str, object]]) -> CallToolResult:
    try:
        result = await operation
        is_error = False
    except TeamsMcpError as exc:
        result = {"error_code": exc.code, "message": str(exc)}
        is_error = True
    except OSError:
        result = {
            "error_code": "file_state_error",
            "message": "Local file state could not be saved or read; do not retry with a new ID.",
        }
        is_error = True
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        structuredContent=result,
        isError=is_error,
    )


def _save_receipt(path: Path, value: dict[str, Any]) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


class SelfFileSender:
    """Send one PDF to self, retaining an operation receipt before every mutation.

    This uses the existing write gate and session. Receipts contain hashes and
    opaque delivery identifiers, never PDF bytes, file paths, names, or tokens.
    A request ID whose network outcome is unknown is never automatically resent.
    """

    def __init__(
        self,
        api: TeamsApi,
        settings: Settings,
        *,
        file_api: TeamsFileApi | None = None,
        receipt_dir: Path | None = None,
    ) -> None:
        self.api = api
        self.settings = settings
        self.files = file_api or TeamsFileApi(api)
        self.receipt_dir = receipt_dir

    async def send(
        self, chat_id: str, file_path: str, request_id: str, *, confirm: bool
    ) -> dict[str, object]:
        if chat_id != SELF_CHAT:
            raise WriteRejectedError(
                "File attachments currently support the authenticated self chat only.",
                "file_target_unsupported",
            )
        supplied_name = Path(file_path).name
        if any(0xD800 <= ord(char) <= 0xDFFF for char in supplied_name):
            raise WriteRejectedError("Use a valid local PDF filename.", "file_path_invalid")
        authorized = WriteGuard(self.settings).authorize(
            target=f"chat:{chat_id}", content=supplied_name, confirm=confirm
        )
        try:
            operation_id = str(UUID(request_id))
        except (ValueError, AttributeError) as exc:
            raise WriteRejectedError("request_id must be a UUID.", "request_id_invalid") from exc
        filename, data = _pdf_bytes(
            file_path, min(MAX_FILE_BYTES, self.settings.max_response_bytes)
        )
        digest = hashlib.sha256(data).hexdigest()
        bundle = await self.api.auth.get_bundle()
        principal = _self_mri(bundle)
        self.files.bind_identity(bundle.tenant_id, principal)
        fingerprint = hashlib.sha256(
            "\0".join((bundle.tenant_id, principal, chat_id, filename, digest)).encode("utf-8")
        ).hexdigest()
        root = self.receipt_dir
        if root is None:
            root = user_cache_path("teams-playwright-mcp-server", "ma-nakaya") / "file-receipts"
        root.mkdir(parents=True, exist_ok=True)
        receipt_path = root / f"{operation_id}.json"
        lock = FileLock(str(root / f"{operation_id}.lock"), timeout=0)
        try:
            with lock:
                return await self._send_locked(
                    receipt_path, fingerprint, filename, data, digest, authorized.content
                )
        except Timeout as exc:
            raise TeamsMcpError(
                "This file send is already in progress.", "send_in_progress"
            ) from exc

    async def _send_locked(
        self,
        receipt_path: Path,
        fingerprint: str,
        filename: str,
        data: bytes,
        digest: str,
        caption: str,
    ) -> dict[str, object]:
        receipt: dict[str, Any]
        if receipt_path.exists():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise TeamsMcpError("The send receipt is unreadable.", "receipt_invalid") from exc
            if not isinstance(receipt, dict) or receipt.get("fingerprint") != fingerprint:
                raise TeamsMcpError(
                    "This request ID belongs to another file or identity.", "request_id_conflict"
                )
            if (
                type(receipt.get("version")) is not int
                or receipt["version"] != 1
                or receipt.get("phase")
                not in {
                    "create_attempted",
                    "created",
                    "upload_attempted",
                    "post_attempted",
                    "verified",
                }
            ):
                raise TeamsMcpError("The send receipt has an unsupported state.", "receipt_invalid")
            object_id = receipt.get("object_id")
            client_id = receipt.get("client_message_id")
            if (
                isinstance(object_id, str)
                and isinstance(client_id, str)
                and receipt.get("creation_policy") == "self_read_v1"
            ):
                # Fingerprint above binds this local receipt to the current
                # authenticated tenant, principal, filename and exact bytes.
                await self.files.restore_created_file(object_id, filename)
                message_id = await self.files.find_file(client_id, object_id, filename)
                if message_id is not None:
                    return await self._verified_result(
                        receipt_path, receipt, object_id, filename, data, digest, message_id
                    )
            raise TeamsMcpError(
                "An earlier attempt has no verified delivery. Do not resend with a new request ID.",
                "send_outcome_unknown",
            )

        receipt = {
            "version": 1,
            "fingerprint": fingerprint,
            "phase": "create_attempted",
            "client_message_id": str(time.time_ns() // 1_000_000),
        }
        _save_receipt(receipt_path, receipt)
        object_id = await self.files.create_file(filename)
        receipt.update(object_id=object_id, phase="created", creation_policy="self_read_v1")
        _save_receipt(receipt_path, receipt)
        await self.files.verify_created_file(object_id)
        receipt["phase"] = "upload_attempted"
        _save_receipt(receipt_path, receipt)
        await self.files.upload_file(object_id, data)
        if await self.files.read_file(object_id) != data:
            raise TeamsMcpError("Uploaded PDF verification failed.", "file_verification_failed")
        receipt["phase"] = "post_attempted"
        _save_receipt(receipt_path, receipt)
        await self.files.post_file(
            object_id, filename, len(data), receipt["client_message_id"], caption=caption
        )
        message_id = await self.files.find_file(receipt["client_message_id"], object_id, filename)
        if message_id is None:
            raise TeamsMcpError(
                "Teams accepted the request but delivery is not yet verified; do not resend.",
                "send_outcome_unknown",
            )
        return await self._verified_result(
            receipt_path, receipt, object_id, filename, data, digest, message_id
        )

    async def _verified_result(
        self,
        receipt_path: Path,
        receipt: dict[str, Any],
        object_id: str,
        filename: str,
        data: bytes,
        digest: str,
        message_id: str,
    ) -> dict[str, object]:
        await self.files.verify_created_file(object_id)
        if await self.files.read_file(object_id) != data:
            raise TeamsMcpError("Attached PDF verification failed.", "file_verification_failed")
        receipt.update(phase="verified", message_id=message_id)
        _save_receipt(receipt_path, receipt)
        logger.info(
            "teams_file_verified target=chat:%s size=%d sha256=%s", SELF_CHAT, len(data), digest
        )
        return {
            "status": "sent_and_verified",
            "target": f"chat:{SELF_CHAT}",
            "file_name": filename,
            "size_bytes": len(data),
            "sha256": digest,
            "message_id": message_id,
            "attachment_verified": True,
            "read_state_changed": False,
        }
