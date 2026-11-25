from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from teams_mcp.api import TeamsApi
from teams_mcp.config import Settings
from teams_mcp.downloads import SharePointDownloader, safe_file_name
from teams_mcp.errors import ApiError

FILE_URL = "https://contoso.sharepoint.com/sites/eng/Shared%20Documents/plan%20v2.pdf"
DOWNLOAD_URL = "https://contoso.sharepoint.com/_layouts/15/download.aspx?UniqueId=1&tempauth=x"
BODY = b"%PDF-1.7 synthetic"


class FakeAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.hosts: list[tuple[str, bool]] = []

    async def get_sharepoint_token(self, host: str, *, force: bool = False) -> str:
        self.hosts.append((host, force))
        return "sp-refreshed" if force else "sp-secret"


def make(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    limit: int = 1024 * 1024,
) -> tuple[SharePointDownloader, FakeAuth, httpx.AsyncClient]:
    settings = Settings(download_dir=str(tmp_path), max_download_bytes=limit)
    auth = FakeAuth(settings)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = TeamsApi(auth, settings, http=http)  # type: ignore[arg-type]
    return SharePointDownloader(api, settings), auth, http


def share_path(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"/_api/v2.0/shares/u!{encoded}/driveItem/content"


def redirecting_handler(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        if request.url.path.startswith("/_api/"):
            return httpx.Response(302, headers={"Location": DOWNLOAD_URL})
        return httpx.Response(
            200,
            content=BODY,
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": "attachment; filename*=UTF-8''plan%20v2.pdf",
            },
        )

    return handler


@pytest.mark.asyncio
async def test_downloads_through_share_endpoint_without_leaking_token(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    downloader, auth, http = make(tmp_path, redirecting_handler(seen))
    async with http:
        result = await downloader.download(FILE_URL + "?share=tok#frag")
    assert seen[0].url.path == share_path(FILE_URL)
    assert seen[0].headers["Authorization"] == "Bearer sp-secret"
    assert "Authorization" not in seen[1].headers
    assert auth.hosts == [("contoso.sharepoint.com", False)]
    assert result["status"] == "downloaded"
    assert result["storage"] == "sharepoint"
    assert result["file_name"] == "plan v2.pdf"
    assert result["size_bytes"] == len(BODY)
    assert result["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert Path(str(result["path"])).read_bytes() == BODY
    assert [p.name for p in tmp_path.iterdir()] == ["plan v2.pdf"]


@pytest.mark.asyncio
async def test_existing_files_are_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "plan v2.pdf").write_bytes(b"keep")
    downloader, _, http = make(tmp_path, redirecting_handler([]))
    async with http:
        result = await downloader.download(FILE_URL)
    assert result["file_name"] == "plan v2 (1).pdf"
    assert (tmp_path / "plan v2.pdf").read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_rejects_non_sharepoint_urls_before_any_request(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    downloader, auth, http = make(tmp_path, handler)
    async with http:
        for url in ("https://evil.example/a.pdf", "http://contoso.sharepoint.com/a.pdf"):
            with pytest.raises(ApiError) as error:
                await downloader.download(url)
            assert error.value.code == "download_url_rejected"
    assert auth.hosts == []


@pytest.mark.asyncio
async def test_rejects_cross_host_redirect(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    downloader, _, http = make(tmp_path, handler)
    async with http:
        with pytest.raises(ApiError) as error:
            await downloader.download(FILE_URL)
    assert error.value.code == "download_redirect_rejected"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_forbidden_download_is_reported_and_leaves_no_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "accessDenied"}})

    downloader, _, http = make(tmp_path, handler)
    async with http:
        with pytest.raises(ApiError) as error:
            await downloader.download(FILE_URL)
    assert error.value.code == "download_forbidden"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_expired_token_is_refreshed_once(tmp_path: Path) -> None:
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["Authorization"])
        if len(tokens) == 1:
            return httpx.Response(401)
        return httpx.Response(200, content=BODY)

    downloader, auth, http = make(tmp_path, handler)
    async with http:
        result = await downloader.download(FILE_URL)
    assert tokens == ["Bearer sp-secret", "Bearer sp-refreshed"]
    assert auth.hosts[-1] == ("contoso.sharepoint.com", True)
    assert result["file_name"] == "plan v2.pdf"


@pytest.mark.asyncio
async def test_oversized_download_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    downloader, _, http = make(tmp_path, handler, limit=1024)
    async with http:
        with pytest.raises(ApiError) as error:
            await downloader.download(FILE_URL)
    assert error.value.code == "download_too_large"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_unwritable_download_dir_is_reported_as_download_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_bytes(b"")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    settings = Settings(download_dir=str(blocker / "sub"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = TeamsApi(FakeAuth(settings), settings, http=http)  # type: ignore[arg-type]
        with pytest.raises(ApiError) as error:
            await SharePointDownloader(api, settings).download(FILE_URL)
    assert error.value.code == "download_save_failed"


def test_safe_file_name_cannot_escape_or_use_reserved_names() -> None:
    assert safe_file_name("..\\..\\evil.pdf") == "_.._evil.pdf"
    assert safe_file_name("a/b:c?.docx") == "a_b_c_.docx"
    assert safe_file_name("CON.txt") == "_CON.txt"
    assert safe_file_name("") == "attachment"
    assert safe_file_name(" . ") == "attachment"
    long_name = safe_file_name("x" * 500 + ".step")
    assert long_name.endswith(".step") and len(long_name) <= 180
