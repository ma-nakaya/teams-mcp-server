"""Read-only download of SharePoint/OneDrive files referenced by Teams messages.

Only GET requests are sent, and only to the *.sharepoint.com host named by the
URL. The delegated SharePoint token carries broader scopes than reading, so this
module must never gain a non-GET request. Redirects are followed only to the
same host and never receive the bearer token.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
from email.message import Message as HeaderMessage
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .api import TeamsApi
from .config import Settings
from .errors import ApiError
from .file_markup import sharepoint_location

_MAX_REDIRECTS = 3
# Filesystems allow 255 bytes (POSIX) or 255 UTF-16 units (NTFS) per name. UTF-8
# bytes are never fewer than UTF-16 units, so a UTF-8 cap satisfies both. The
# remaining room covers the " (999)" collision suffix added by _store().
_MAX_NAME_BYTES = 200
_MAX_SUFFIX_BYTES = 20
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _share_id(url: str) -> str:
    """Encode an absolute URL as a SharePoint sharing identifier (u!<base64url>)."""
    return "u!" + base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def safe_file_name(value: str | None) -> str:
    """Return a local file name that cannot escape the download directory."""
    name = _UNSAFE_NAME.sub("_", value or "").strip(" .")
    if not name:
        name = "attachment"
    # Windows treats "CON.report.pdf" and "COM1 .txt" as the device, so check the
    # part before the first dot with trailing spaces removed.
    if name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES:
        name = f"_{name}"
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    suffix = _truncate_utf8(suffix, _MAX_SUFFIX_BYTES)
    reserved = len(suffix.encode("utf-8")) + 1 if suffix else 0
    stem = _truncate_utf8(stem, _MAX_NAME_BYTES - reserved) or "attachment"
    return f"{stem}.{suffix}" if suffix else stem


def _truncate_utf8(value: str, limit: int) -> str:
    """Cut to at most `limit` UTF-8 bytes without splitting a character."""
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _disposition_name(response: httpx.Response) -> str | None:
    header = response.headers.get("Content-Disposition")
    if not header:
        return None
    parsed = HeaderMessage()
    parsed["Content-Disposition"] = header
    name = parsed.get_filename()
    return name if isinstance(name, str) and name else None


def _url_name(url: str) -> str | None:
    segment = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(segment) or None


def mark_from_internet(path: Path, source_url: str) -> bool:
    """Attach the Windows Mark of the Web, as a browser download would.

    Office and SmartScreen rely on it to block macros and active content from
    the Internet. Without it the file must not be handed over, so failure stops
    the download. Returns False on platforms without zone identifiers.
    """
    if os.name != "nt":
        return False
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="utf-8") as stream:
            stream.write(f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={source_url}\r\n")
    except OSError as exc:
        raise ApiError(
            "The download could not be marked as coming from the Internet, so it was not "
            "saved. Use a TEAMS_DOWNLOAD_DIR on an NTFS volume.",
            "download_motw_failed",
        ) from exc
    return True


def _store(directory: Path, name: str, temporary: Path) -> Path:
    """Move a finished download into place without replacing an existing file."""
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    for index in range(1000):
        candidate_name = name if index == 0 else f"{stem} ({index}){dot}{suffix}"
        candidate = directory / candidate_name
        try:
            os.link(temporary, candidate)
        except FileExistsError:
            continue
        return candidate
    raise ApiError("Could not choose a free local file name.", "download_name_exhausted")


class SharePointDownloader:
    def __init__(self, api: TeamsApi, settings: Settings | None = None) -> None:
        self.api = api
        self.settings = settings or api.settings

    async def download(self, url: str) -> dict[str, object]:
        location = sharepoint_location(url)
        if location is None:
            raise ApiError(
                "Only HTTPS SharePoint/OneDrive URLs from Teams attachments can be downloaded.",
                "download_url_rejected",
            )
        clean_url, storage = location
        host = urlsplit(clean_url).hostname or ""
        endpoint = f"https://{host}/_api/v2.0/shares/{_share_id(clean_url)}/driveItem/content"
        directory = Path(self.settings.download_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handle, raw_temporary = tempfile.mkstemp(prefix=".teams-download-", dir=directory)
            os.close(handle)
            temporary = Path(raw_temporary)
            try:
                size, digest, content_type, name = await self._fetch(host, endpoint, temporary)
                # Hard links share alternate data streams, so marking the temporary
                # file marks the saved one before it ever becomes visible.
                marked = mark_from_internet(temporary, clean_url)
                stored = _store(directory, safe_file_name(name or _url_name(clean_url)), temporary)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise ApiError(
                "The file could not be saved to TEAMS_DOWNLOAD_DIR.", "download_save_failed"
            ) from exc
        return {
            "status": "downloaded",
            "storage": storage,
            "path": str(stored),
            "file_name": stored.name,
            "size_bytes": size,
            "sha256": digest,
            "content_type": content_type,
            "marked_from_internet": marked,
        }

    async def _fetch(
        self, host: str, url: str, destination: Path
    ) -> tuple[int, str, str | None, str | None]:
        token = await self.api.auth.get_sharepoint_token(host)
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        retried_auth = False
        redirects = 0
        while True:
            try:
                async with self.api._client().stream(
                    "GET", url, headers=headers, follow_redirects=False
                ) as response:
                    if response.status_code == 401 and headers and not retried_auth:
                        token = await self.api.auth.get_sharepoint_token(host, force=True)
                        headers = {"Authorization": f"Bearer {token}"}
                        retried_auth = True
                        continue
                    if response.is_redirect:
                        redirects += 1
                        url = self._redirect_target(url, response, host, redirects)
                        headers = {}  # pre-authorized download URLs must not receive the token
                        continue
                    self._raise_for_status(response)
                    return await self._write(response, destination)
            except httpx.HTTPError as exc:
                # The message can include the signed URL, so it is not passed through.
                raise ApiError(
                    "The SharePoint download failed at the network layer.",
                    "download_network_error",
                ) from exc

    @staticmethod
    def _redirect_target(url: str, response: httpx.Response, host: str, redirects: int) -> str:
        location = response.headers.get("Location")
        target = urljoin(url, location) if location else ""
        parsed = urlsplit(target)
        if (
            redirects > _MAX_REDIRECTS
            or parsed.scheme != "https"
            or (parsed.hostname or "").lower() != host
        ):
            raise ApiError(
                "SharePoint download redirect was refused.", "download_redirect_rejected"
            )
        return target

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code == 403:
            raise ApiError(
                "SharePoint refused the download. The file may block downloads "
                "(for example, a meeting recording) or you may lack access.",
                "download_forbidden",
                status_code=403,
            )
        if response.status_code == 404:
            raise ApiError(
                "The SharePoint file was not found.", "download_not_found", status_code=404
            )
        raise ApiError(
            f"SharePoint returned HTTP {response.status_code}.",
            f"download_http_{response.status_code}",
            status_code=response.status_code,
        )

    async def _write(
        self, response: httpx.Response, destination: Path
    ) -> tuple[int, str, str | None, str | None]:
        limit = self.settings.max_download_bytes
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ApiError("The file exceeds TEAMS_MAX_DOWNLOAD_BYTES.", "download_too_large")
        digest = hashlib.sha256()
        size = 0
        with destination.open("wb") as stream:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise ApiError(
                        "The file exceeds TEAMS_MAX_DOWNLOAD_BYTES.", "download_too_large"
                    )
                digest.update(chunk)
                stream.write(chunk)
        content_type = response.headers.get("Content-Type")
        return size, digest.hexdigest(), content_type, _disposition_name(response)
