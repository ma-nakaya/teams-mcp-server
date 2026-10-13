from __future__ import annotations

import base64
import hashlib
import json
import logging
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

import teams_mcp.files as files_module
from teams_mcp.config import Settings
from teams_mcp.errors import ApiError, TeamsMcpError, WriteRejectedError
from teams_mcp.files import MAX_FILE_BYTES, SELF_CHAT, SelfFileSender, _pdf_bytes, file_tool_result
from teams_mcp.service import TeamsService

PDF_BYTES = b"%PDF-1.7\nSynthetic private attachment content only.\n%%EOF\n"
TENANT_ID = "11111111-2222-4333-8444-555555555555"
PRINCIPAL = "orgid:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def synthetic_token() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"skypeid": PRINCIPAL}).encode()).decode()
    return f"synthetic-header.{payload.rstrip('=')}.synthetic-signature"


class FakeAuth:
    def __init__(self) -> None:
        self.calls = 0
        self.bundle = SimpleNamespace(skype_token=synthetic_token(), tenant_id=TENANT_ID)

    async def get_bundle(self) -> SimpleNamespace:
        self.calls += 1
        return self.bundle


class FakeApi:
    def __init__(self) -> None:
        self.auth = FakeAuth()

    async def set_consumption_horizon(self, *_: object, **__: object) -> None:
        raise AssertionError("File delivery must not change read state")


class FakeFileApi:
    """Offline transport; every operation records the persisted receipt phase."""

    def __init__(self, receipt_dir: Path) -> None:
        self.receipt_dir = receipt_dir
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.phases: list[tuple[str, str | None]] = []
        self.fail_at: str | None = None
        self.reject_permissions_at: int | None = None
        self.permission_checks = 0
        self.read_returns: list[bytes] = []
        self.data: bytes | None = None
        self.posted: tuple[str, str, str] | None = None
        self.post_filename_override: str | None = None
        self.delivery_visible = True
        self.accept_post_then_fail = False
        self.object_id = "synthetic-object-123"
        self.message_id = "1700000000001"
        self.bound_identity: tuple[str, str] | None = None

    def record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        receipts = list(self.receipt_dir.glob("*.json"))
        phase = json.loads(receipts[0].read_text())["phase"] if receipts else None
        self.phases.append((name, phase))
        if self.fail_at == name:
            raise ApiError("Synthetic offline transport failure", "synthetic_failure")

    def bind_identity(self, tenant_id: str, principal: str) -> None:
        self.bound_identity = (tenant_id, principal)

    async def create_file(self, filename: str) -> str:
        self.record("create", filename)
        return self.object_id

    async def restore_created_file(self, object_id: str, filename: str) -> None:
        self.record("restore", object_id, filename)

    async def verify_created_file(self, object_id: str) -> None:
        self.record("permissions", object_id)
        self.permission_checks += 1
        if self.permission_checks == self.reject_permissions_at:
            raise ApiError("Synthetic permission mismatch", "file_permissions_invalid")

    async def upload_file(self, object_id: str, data: bytes) -> None:
        self.record("upload", object_id, data)
        self.data = data

    async def read_file(self, object_id: str) -> bytes:
        self.record("read", object_id)
        if self.read_returns:
            return self.read_returns.pop(0)
        assert self.data is not None
        return self.data

    async def post_file(
        self, object_id: str, filename: str, size: int, client_id: str, *, caption: str
    ) -> None:
        self.record("post", object_id, filename, size, client_id, caption)
        self.posted = (client_id, object_id, self.post_filename_override or filename)
        if self.accept_post_then_fail:
            raise ApiError("Synthetic lost post response", "synthetic_failure")

    async def find_file(self, client_id: str, object_id: str, filename: str) -> str | None:
        self.record("find", client_id, object_id, filename)
        if self.delivery_visible and self.posted == (client_id, object_id, filename):
            return self.message_id
        return None

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def mutation_counts(self) -> tuple[int, int, int]:
        names = self.names()
        return tuple(names.count(name) for name in ("create", "upload", "post"))


@pytest.fixture
def allowed_settings() -> Settings:
    return Settings(
        writes_enabled=True,
        write_allowlist=frozenset({f"chat:{SELF_CHAT}"}),
        read_state_writes_enabled=False,
        read_state_allowlist=frozenset(),
        agent_marker="",
    )


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic-private-申出書.pdf"
    path.write_bytes(PDF_BYTES)
    return path


@pytest.fixture
def harness(tmp_path: Path, allowed_settings: Settings) -> SimpleNamespace:
    receipts = tmp_path / "receipts"
    api = FakeApi()
    transport = FakeFileApi(receipts)
    sender = SelfFileSender(api, allowed_settings, file_api=transport, receipt_dir=receipts)
    return SimpleNamespace(
        sender=sender, api=api, files=transport, receipts=receipts, request_id=str(uuid4())
    )


@pytest.mark.parametrize(
    ("settings_change", "confirm", "expected_code"),
    [
        ({"writes_enabled": False}, True, "writes_disabled"),
        ({"write_allowlist": frozenset()}, True, "target_not_allowlisted"),
        ({}, False, "confirmation_required"),
    ],
)
async def test_write_guard_runs_before_file_read_auth_or_upload(
    harness: SimpleNamespace,
    allowed_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    settings_change: dict[str, Any],
    confirm: bool,
    expected_code: str,
) -> None:
    harness.sender.settings = replace(allowed_settings, **settings_change)
    reader = Mock(side_effect=AssertionError("Guard must precede local file access"))
    monkeypatch.setattr(files_module, "_pdf_bytes", reader)
    with pytest.raises(WriteRejectedError) as caught:
        await harness.sender.send(
            SELF_CHAT, "unread-private.pdf", harness.request_id, confirm=confirm
        )
    assert caught.value.code == expected_code
    reader.assert_not_called()
    assert harness.api.auth.calls == 0
    assert harness.files.calls == []
    assert not harness.receipts.exists()


@pytest.mark.parametrize("chat_id", ["19:other-person", "19:channel", "48:notes ", "48%3Anotes"])
async def test_only_exact_self_chat_is_supported_even_if_other_target_allowlisted(
    harness: SimpleNamespace,
    allowed_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    chat_id: str,
) -> None:
    harness.sender.settings = replace(
        allowed_settings, write_allowlist=frozenset({f"chat:{chat_id}"})
    )
    reader = Mock(side_effect=AssertionError("Other targets cannot read the attachment"))
    monkeypatch.setattr(files_module, "_pdf_bytes", reader)
    with pytest.raises(WriteRejectedError) as caught:
        await harness.sender.send(chat_id, "private.pdf", harness.request_id, confirm=True)
    assert caught.value.code == "file_target_unsupported"
    reader.assert_not_called()
    assert harness.files.calls == []
    assert harness.api.auth.calls == 0


async def test_invalid_request_uuid_is_rejected_before_reading(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Mock(side_effect=AssertionError("Invalid request ID cannot read the file"))
    monkeypatch.setattr(files_module, "_pdf_bytes", reader)
    with pytest.raises(WriteRejectedError) as caught:
        await harness.sender.send(SELF_CHAT, "private.pdf", "not-a-uuid", confirm=True)
    assert caught.value.code == "request_id_invalid"
    reader.assert_not_called()
    assert harness.files.calls == []


@pytest.mark.parametrize(
    "name",
    [
        "relative.pdf",
        r"\\server\share\private.pdf",
        "wrong.txt",
        "stream:other.pdf",
        "bad\n.pdf",
        "delete\x7f.pdf",
        "surrogate\ud800.pdf",
    ],
)
def test_invalid_pdf_paths_are_rejected_before_opening(tmp_path: Path, name: str) -> None:
    path = name if name.startswith(("relative", "\\\\")) else str(tmp_path / name)
    with pytest.raises(WriteRejectedError) as caught:
        _pdf_bytes(path)
    assert caught.value.code == "file_path_invalid"


def test_long_filename_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WriteRejectedError) as caught:
        _pdf_bytes(str(tmp_path / ("x" * 237 + ".pdf")))
    assert caught.value.code == "file_path_invalid"


@pytest.mark.parametrize("name_bytes", [255, 256])
def test_multibyte_filename_utf8_boundary(tmp_path: Path, name_bytes: int) -> None:
    name = "あ" * 83 + "ab.pdf" if name_bytes == 255 else "あ" * 84 + ".pdf"
    assert len(name.encode("utf-8")) == name_bytes
    assert len(name) < 240
    path = tmp_path / name
    if name_bytes == 255:
        path.write_bytes(PDF_BYTES)
        assert _pdf_bytes(str(path)) == (name, PDF_BYTES)
    else:
        with pytest.raises(WriteRejectedError) as caught:
            _pdf_bytes(str(path))
        assert caught.value.code == "file_path_invalid"


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"", "file_size_invalid"),
        (b"not a PDF", "file_format_invalid"),
        (b"%PDF-1.7\nmissing terminator", "file_format_invalid"),
    ],
)
def test_empty_or_incomplete_pdf_is_rejected(
    tmp_path: Path, content: bytes, expected_code: str
) -> None:
    path = tmp_path / "invalid.pdf"
    path.write_bytes(content)
    with pytest.raises(WriteRejectedError) as caught:
        _pdf_bytes(str(path))
    assert caught.value.code == expected_code


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_ten_mib_size_boundary(tmp_path: Path, extra_bytes: int) -> None:
    path = tmp_path / "boundary.pdf"
    data = b"%PDF-1.7\n" + b"x" * (MAX_FILE_BYTES + extra_bytes - 15) + b"%%EOF\n"
    assert len(data) == MAX_FILE_BYTES + extra_bytes
    path.write_bytes(data)
    if extra_bytes:
        with pytest.raises(WriteRejectedError) as caught:
            _pdf_bytes(str(path))
        assert caught.value.code == "file_size_invalid"
    else:
        assert _pdf_bytes(str(path)) == (path.name, data)


def test_unreadable_pdf_has_stable_nonsecret_error(tmp_path: Path) -> None:
    path = tmp_path / "secret-unreadable-name.pdf"
    with pytest.raises(WriteRejectedError) as caught:
        _pdf_bytes(str(path))
    assert caught.value.code == "file_unreadable"
    assert path.name not in str(caught.value)
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize("kind", ["symlink", "parent_reparse"])
def test_linked_files_and_parent_reparse_points_are_rejected(
    sample_pdf: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    original_lstat = Path.lstat

    def controlled_lstat(path: Path) -> Any:
        target = sample_pdf if kind == "symlink" else sample_pdf.parent
        if path == target:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK if kind == "symlink" else stat.S_IFDIR,
                st_file_attributes=0 if kind == "symlink" else 0x400,
            )
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", controlled_lstat)
    with pytest.raises(WriteRejectedError) as caught:
        _pdf_bytes(str(sample_pdf))
    assert caught.value.code == "file_path_invalid"


def test_file_size_change_while_reading_is_rejected(
    sample_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_fstat = files_module.os.fstat

    def changed_stat(fd: int) -> SimpleNamespace:
        info = original_fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size + 1)

    monkeypatch.setattr(files_module.os, "fstat", changed_stat)
    with pytest.raises(WriteRejectedError) as caught:
        _pdf_bytes(str(sample_pdf))
    assert caught.value.code == "file_changed"


@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_configured_lower_size_limit_precedes_receipt_auth_or_create(
    harness: SimpleNamespace,
    sample_pdf: Path,
    allowed_settings: Settings,
    extra_bytes: int,
) -> None:
    maximum = 1024
    harness.sender.settings = replace(allowed_settings, max_response_bytes=maximum)
    data = b"%PDF-1.7\n" + b"x" * (maximum + extra_bytes - 15) + b"%%EOF\n"
    assert len(data) == maximum + extra_bytes
    sample_pdf.write_bytes(data)
    if extra_bytes:
        with pytest.raises(WriteRejectedError) as caught:
            await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
        assert caught.value.code == "file_size_invalid"
        assert not harness.receipts.exists()
        assert harness.api.auth.calls == 0
        assert harness.files.calls == []
    else:
        result = await harness.sender.send(
            SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True
        )
        assert result["status"] == "sent_and_verified"
        assert result["size_bytes"] == maximum
        assert harness.files.data == data


async def test_large_configured_limit_does_not_raise_ten_mib_send_cap(
    harness: SimpleNamespace, sample_pdf: Path, allowed_settings: Settings
) -> None:
    harness.sender.settings = replace(allowed_settings, max_response_bytes=MAX_FILE_BYTES * 2)
    sample_pdf.write_bytes(b"%PDF-1.7\n" + b"x" * (MAX_FILE_BYTES - 14) + b"%%EOF\n")
    assert sample_pdf.stat().st_size == MAX_FILE_BYTES + 1
    with pytest.raises(WriteRejectedError) as caught:
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert caught.value.code == "file_size_invalid"
    assert not harness.receipts.exists()
    assert harness.api.auth.calls == 0
    assert harness.files.calls == []


async def test_success_requires_permissions_bytes_and_exact_attachment_filename(
    harness: SimpleNamespace, sample_pdf: Path
) -> None:
    result = await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert result == {
        "status": "sent_and_verified",
        "target": f"chat:{SELF_CHAT}",
        "file_name": sample_pdf.name,
        "size_bytes": len(PDF_BYTES),
        "sha256": hashlib.sha256(PDF_BYTES).hexdigest(),
        "message_id": harness.files.message_id,
        "attachment_verified": True,
        "read_state_changed": False,
    }
    assert harness.files.names() == [
        "create",
        "permissions",
        "upload",
        "read",
        "post",
        "find",
        "permissions",
        "read",
    ]
    assert harness.files.data == PDF_BYTES
    assert harness.files.calls[0][1] == (sample_pdf.name,)
    assert harness.files.calls[5][1][2] == sample_pdf.name
    mutation_phases = [
        (name, phase)
        for name, phase in harness.files.phases
        if name in {"create", "upload", "post"}
    ]
    assert mutation_phases == [
        ("create", "create_attempted"),
        ("upload", "upload_attempted"),
        ("post", "post_attempted"),
    ]
    receipt = json.loads((harness.receipts / f"{harness.request_id}.json").read_text())
    assert receipt["phase"] == "verified"


@pytest.mark.parametrize("marker", ["", "[CUSTOM]"])
async def test_caption_uses_configured_marker_without_inventing_one(
    harness: SimpleNamespace, sample_pdf: Path, allowed_settings: Settings, marker: str
) -> None:
    harness.sender.settings = replace(allowed_settings, agent_marker=marker)
    await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    post_args = next(args for name, args in harness.files.calls if name == "post")
    assert post_args[-1] == (f"{marker} {sample_pdf.name}" if marker else sample_pdf.name)


async def test_receipt_uuid_reuse_verifies_without_create_upload_or_post(
    harness: SimpleNamespace, sample_pdf: Path
) -> None:
    first = await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert harness.files.mutation_counts() == (1, 1, 1)
    second = await harness.sender.send(
        SELF_CHAT, str(sample_pdf), "{" + harness.request_id.upper() + "}", confirm=True
    )
    assert second == first
    assert harness.files.mutation_counts() == (1, 1, 1)
    assert harness.files.names()[-3:] == ["find", "permissions", "read"]
    assert len(list(harness.receipts.glob("*.json"))) == 1


@pytest.mark.parametrize("failure_phase", ["create", "upload", "post"])
async def test_unknown_mutation_outcome_never_automatically_resends(
    harness: SimpleNamespace, sample_pdf: Path, failure_phase: str
) -> None:
    harness.files.fail_at = failure_phase
    with pytest.raises(ApiError):
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    mutation_counts = harness.files.mutation_counts()
    harness.files.fail_at = None
    with pytest.raises(TeamsMcpError) as caught:
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert caught.value.code == "send_outcome_unknown"
    assert harness.files.mutation_counts() == mutation_counts


async def test_lost_post_response_can_be_verified_without_resending(
    harness: SimpleNamespace, sample_pdf: Path
) -> None:
    harness.files.accept_post_then_fail = True
    with pytest.raises(ApiError):
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    harness.files.accept_post_then_fail = False
    result = await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert result["attachment_verified"] is True
    assert harness.files.mutation_counts() == (1, 1, 1)


@pytest.mark.parametrize("conflict", ["different_bytes", "different_name"])
async def test_request_id_cannot_be_reused_for_another_file(
    harness: SimpleNamespace, sample_pdf: Path, tmp_path: Path, conflict: str
) -> None:
    await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    if conflict == "different_bytes":
        sample_pdf.write_bytes(b"%PDF-1.7\nOther synthetic content\n%%EOF\n")
        next_path = sample_pdf
    else:
        next_path = tmp_path / "different-name.pdf"
        next_path.write_bytes(PDF_BYTES)
    calls_before = list(harness.files.calls)
    with pytest.raises(TeamsMcpError) as caught:
        await harness.sender.send(SELF_CHAT, str(next_path), harness.request_id, confirm=True)
    assert caught.value.code == "request_id_conflict"
    assert harness.files.calls == calls_before


async def test_acl_rejection_precedes_upload_of_any_file_bytes(
    harness: SimpleNamespace, sample_pdf: Path
) -> None:
    harness.files.reject_permissions_at = 1
    with pytest.raises(ApiError) as caught:
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert caught.value.code == "file_permissions_invalid"
    assert harness.files.names() == ["create", "permissions"]
    assert harness.files.data is None


async def test_prepost_byte_mismatch_prevents_chat_post(
    harness: SimpleNamespace, sample_pdf: Path
) -> None:
    harness.files.read_returns = [b"different bytes"]
    with pytest.raises(TeamsMcpError) as caught:
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert caught.value.code == "file_verification_failed"
    assert "post" not in harness.files.names()
    assert "find" not in harness.files.names()


async def test_postdelivery_byte_mismatch_does_not_report_success(
    harness: SimpleNamespace, sample_pdf: Path
) -> None:
    harness.files.read_returns = [PDF_BYTES, b"changed after posting"]
    with pytest.raises(TeamsMcpError) as caught:
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert caught.value.code == "file_verification_failed"
    receipt = json.loads((harness.receipts / f"{harness.request_id}.json").read_text())
    assert receipt["phase"] == "post_attempted"
    assert harness.files.mutation_counts() == (1, 1, 1)


async def test_postdelivery_acl_mismatch_does_not_report_success(
    harness: SimpleNamespace, sample_pdf: Path
) -> None:
    harness.files.reject_permissions_at = 2
    with pytest.raises(ApiError) as caught:
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert caught.value.code == "file_permissions_invalid"
    receipt = json.loads((harness.receipts / f"{harness.request_id}.json").read_text())
    assert receipt["phase"] == "post_attempted"


@pytest.mark.parametrize("mismatch", ["not_visible", "different_filename"])
async def test_missing_exact_attachment_evidence_remains_unknown_without_resend(
    harness: SimpleNamespace, sample_pdf: Path, mismatch: str
) -> None:
    if mismatch == "not_visible":
        harness.files.delivery_visible = False
    else:
        harness.files.post_filename_override = "another-name.pdf"
    for _ in range(2):
        with pytest.raises(TeamsMcpError) as caught:
            await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
        assert caught.value.code == "send_outcome_unknown"
    assert harness.files.mutation_counts() == (1, 1, 1)


async def test_receipt_and_audit_never_persist_filename_path_content_or_token(
    harness: SimpleNamespace, sample_pdf: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="teams_mcp.audit"):
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    receipt_text = "\n".join(path.read_text() for path in harness.receipts.glob("*.json"))
    forbidden = [
        sample_pdf.name,
        str(sample_pdf),
        PDF_BYTES.decode(),
        "Synthetic private attachment content only.",
        harness.api.auth.bundle.skype_token,
        PRINCIPAL,
    ]
    for private_value in forbidden:
        assert private_value not in receipt_text
        assert private_value not in caplog.text
    assert "teams_file_verified" in caplog.text
    assert "teams_write_authorized" in caplog.text
    receipt = json.loads(receipt_text)
    assert set(receipt) == {
        "version",
        "fingerprint",
        "phase",
        "client_message_id",
        "object_id",
        "message_id",
        "creation_policy",
    }


async def test_file_tool_result_preserves_structured_api_error_code() -> None:
    async def rejected() -> dict[str, object]:
        raise ApiError(
            "Synthetic permissions rejected",
            "file_permissions_invalid",
            status_code=403,
            request_id="synthetic-private-request-id",
        )

    result = await file_tool_result(rejected())
    expected = {
        "error_code": "file_permissions_invalid",
        "message": "Synthetic permissions rejected",
    }
    assert result.isError is True
    assert result.structuredContent == expected
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == expected
    assert "synthetic-private-request-id" not in result.model_dump_json()


async def test_file_tool_result_strips_oserror_secret_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_path = r"C:\synthetic-private-folder\sensitive-name.pdf"

    async def failed_local_state() -> dict[str, object]:
        raise PermissionError(13, "Synthetic private OS failure details", private_path)

    with caplog.at_level(logging.INFO, logger="teams_mcp.audit"):
        result = await file_tool_result(failed_local_state())
    expected = {
        "error_code": "file_state_error",
        "message": "Local file state could not be saved or read; do not retry with a new ID.",
    }
    assert result.isError is True
    assert result.structuredContent == expected
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == expected
    for private_value in (
        private_path,
        "sensitive-name.pdf",
        "Synthetic private OS failure details",
    ):
        assert private_value not in result.model_dump_json()
        assert private_value not in caplog.text


async def test_file_tool_result_preserves_success_payload() -> None:
    expected = {"status": "sent_and_verified", "attachment_verified": True}

    async def succeeded() -> dict[str, object]:
        return expected

    result = await file_tool_result(succeeded())
    assert result.isError is False
    assert result.structuredContent == expected
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("creation_policy", None),
        ("creation_policy", "unknown_policy"),
        ("fingerprint", "wrong-fingerprint"),
        ("version", 2),
        ("version", True),
        ("phase", "corrupt-phase"),
        ("object_id", 123),
        ("client_message_id", 123),
    ],
)
async def test_invalid_or_untrusted_receipt_never_restores_or_verifies(
    harness: SimpleNamespace, sample_pdf: Path, field: str, value: object
) -> None:
    await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    receipt_path = harness.receipts / f"{harness.request_id}.json"
    receipt = json.loads(receipt_path.read_text())
    if value is None:
        del receipt[field]
    else:
        receipt[field] = value
    receipt_path.write_text(json.dumps(receipt))
    harness.files.calls.clear()
    harness.files.phases.clear()
    with pytest.raises(TeamsMcpError):
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert harness.files.calls == []


@pytest.mark.parametrize("corrupt", ["not JSON", "[]"])
async def test_corrupt_receipt_body_never_restores_or_verifies(
    harness: SimpleNamespace, sample_pdf: Path, corrupt: str
) -> None:
    await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    receipt_path = harness.receipts / f"{harness.request_id}.json"
    receipt_path.write_text(corrupt)
    harness.files.calls.clear()
    harness.files.phases.clear()
    with pytest.raises(TeamsMcpError):
        await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert harness.files.calls == []


async def test_new_adapter_reconciles_accepted_send_without_any_remote_mutation(
    harness: SimpleNamespace, sample_pdf: Path, allowed_settings: Settings
) -> None:
    first = await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    new_api = FakeApi()
    new_adapter = FakeFileApi(harness.receipts)
    # These two fields represent persisted remote state, not process-local trust.
    new_adapter.data = harness.files.data
    new_adapter.posted = harness.files.posted
    restarted_sender = SelfFileSender(
        new_api, allowed_settings, file_api=new_adapter, receipt_dir=harness.receipts
    )
    second = await restarted_sender.send(
        SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True
    )
    assert second == first
    assert new_adapter.bound_identity == (TENANT_ID, "8:" + PRINCIPAL)
    assert new_adapter.names() == ["restore", "find", "permissions", "read"]
    assert new_adapter.calls[0][1] == (harness.files.object_id, sample_pdf.name)
    assert new_adapter.mutation_counts() == (0, 0, 0)
    assert harness.files.mutation_counts() == (1, 1, 1)


async def test_receipt_from_another_tenant_cannot_restore_object_trust(
    harness: SimpleNamespace, sample_pdf: Path, allowed_settings: Settings
) -> None:
    await harness.sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    new_api = FakeApi()
    new_api.auth.bundle.tenant_id = "99999999-2222-4333-8444-555555555555"
    new_adapter = FakeFileApi(harness.receipts)
    restarted_sender = SelfFileSender(
        new_api, allowed_settings, file_api=new_adapter, receipt_dir=harness.receipts
    )
    with pytest.raises(TeamsMcpError) as caught:
        await restarted_sender.send(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert caught.value.code == "request_id_conflict"
    assert new_adapter.calls == []


async def test_service_delegates_attachment_to_guarded_sender(
    harness: SimpleNamespace,
    sample_pdf: Path,
    allowed_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"status": "sent_and_verified", "attachment_verified": True}
    send = AsyncMock(return_value=expected)
    sender_factory = Mock(return_value=SimpleNamespace(send=send))
    monkeypatch.setattr("teams_mcp.service.SelfFileSender", sender_factory)
    service = TeamsService(harness.api.auth, harness.api, allowed_settings)
    result = await service.send_chat_file(
        SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True
    )
    assert result == expected
    sender_factory.assert_called_once_with(harness.api, allowed_settings)
    send.assert_awaited_once_with(SELF_CHAT, str(sample_pdf), harness.request_id, confirm=True)
    assert harness.files.calls == []
