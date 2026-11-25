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
_MAX_NAME = 180
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
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    if stem.upper() in _RESERVED_NAMES:
        stem = f"_{stem}"
    suffix = suffix[:20]
    stem = stem[: _MAX_NAME - len(suffix) - 1] if suffix else stem[:_MAX_NAME]
    return f"{stem}.{suffix}" if suffix else stem


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
