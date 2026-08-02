from __future__ import annotations

import hashlib
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.errors import ArchiveError, ErrorClass


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size: int
    resumed: bool
    attempts: int


class DirectDownloader:
    """Stream an archive to an attempt-specific path.

    The downloader never writes the registered final artifact. A caller may
    atomically promote the returned temporary file only after validation and
    fencing checks succeed.
    """

    def __init__(
        self,
        *,
        session: Any | None = None,
        timeout: tuple[float, float] = (30.0, 120.0),
        retries: int = 3,
        backoff: float = 2.0,
        jitter: float = 0.25,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        self.session = session
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.jitter = jitter
        self.chunk_size = chunk_size

    def download(
        self,
        url: str,
        destination: str | Path,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        proxies: dict[str, str] | None = None,
        expected_size: int | None = None,
        max_size: int | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> DownloadResult:
        if self.session is None:
            import requests

            session = requests.Session()
        else:
            session = self.session
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            existing = part.stat().st_size if part.exists() else 0
            if expected_size and existing >= expected_size:
                if existing == expected_size:
                    os.replace(part, destination)
                    return DownloadResult(destination, existing, True, attempt - 1)
                part.unlink(missing_ok=True)
                existing = 0
            request_headers = dict(headers or {})
            if existing:
                request_headers["Range"] = f"bytes={existing}-"
            try:
                response = session.get(
                    url,
                    headers=request_headers,
                    cookies=cookies,
                    proxies=proxies,
                    stream=True,
                    timeout=self.timeout,
                )
                status = int(response.status_code)
                if status in {404, 410}:
                    raise ArchiveError(
                        "archive_unavailable", f"download returned HTTP {status}", ErrorClass.ITEM
                    )
                if status in {408, 425, 429, 500, 502, 503, 504}:
                    raise ArchiveError(
                        "download_http_temporary",
                        f"download returned HTTP {status}",
                        ErrorClass.TEMPORARY,
                        retryable=True,
                    )
                if status == 416 and existing:
                    part.unlink(missing_ok=True)
                    raise OSError("server rejected the resume range")
                response.raise_for_status()
                resumed = existing > 0 and status == 206
                if existing and not resumed:
                    existing = 0
                    part.unlink(missing_ok=True)
                mode = "ab" if resumed else "wb"
                expected_response = response.headers.get("content-length")
                expected_total = expected_size or (
                    int(expected_response) + existing
                    if expected_response and resumed
                    else int(expected_response)
                    if expected_response
                    else None
                )
                written = existing
                with part.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=self.chunk_size):
                        if not chunk:
                            continue
                        if max_size is not None and written + len(chunk) > max_size:
                            raise ArchiveError(
                                "artifact_too_large",
                                f"download exceeds configured limit {max_size}",
                                ErrorClass.ITEM,
                            )
                        handle.write(chunk)
                        written += len(chunk)
                        if progress:
                            progress(len(chunk))
                    handle.flush()
                    os.fsync(handle.fileno())
                if expected_total is not None and written != expected_total:
                    raise OSError(
                        f"download size mismatch: expected {expected_total}, got {written}"
                    )
                os.replace(part, destination)
                return DownloadResult(destination, written, resumed, attempt)
            except ArchiveError as exc:
                if not exc.info.retryable or attempt >= self.retries:
                    raise
                last_error = exc
                time.sleep(self.backoff ** (attempt - 1) + random.random() * self.jitter)
            except Exception as exc:  # noqa: BLE001 - adapters expose different request exception classes
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(self.backoff ** (attempt - 1) + random.random() * self.jitter)
        message = str(last_error or "download failed")
        message = re.sub(r"https?://[^\s)]+", "<redacted-url>", message)
        raise ArchiveError(
            "download_failed",
            message,
            ErrorClass.TEMPORARY,
            retryable=True,
        )


def file_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
