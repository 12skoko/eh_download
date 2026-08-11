from __future__ import annotations

import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.errors import ArchiveError, ErrorClass, classify_exception


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
        role: str | None = None,
    ) -> None:
        self.session = session
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.jitter = jitter
        self.chunk_size = chunk_size
        self.role = role

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
            if progress:
                # Report the absolute size so retries and resumed transfers do
                # not make the caller double-count previously written bytes.
                progress(existing)
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
                request_kwargs = {
                    "headers": request_headers,
                    "cookies": cookies,
                    "proxies": proxies,
                    "stream": True,
                    "timeout": self.timeout,
                }
                if self.role is not None:
                    request_kwargs["role"] = self.role
                response = session.get(
                    url,
                    **request_kwargs,
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
                if status in {401, 403}:
                    raise ArchiveError(
                        "eh_authentication_failed",
                        f"archive download returned HTTP {status}",
                        ErrorClass.SYSTEM,
                    )
                if status == 416 and existing:
                    part.unlink(missing_ok=True)
                    raise ArchiveError(
                        "download_resume_rejected",
                        "server rejected the resume range",
                        ErrorClass.TEMPORARY,
                        retryable=True,
                    )
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
                            progress(written)
                    handle.flush()
                    os.fsync(handle.fileno())
                if expected_total is not None and written != expected_total:
                    raise ArchiveError(
                        "download_size_mismatch",
                        f"download size mismatch: expected {expected_total}, got {written}",
                        ErrorClass.TEMPORARY,
                        retryable=True,
                    )
                os.replace(part, destination)
                return DownloadResult(destination, written, resumed, attempt)
            except ArchiveError as exc:
                if not exc.info.retryable or attempt >= self.retries:
                    raise
                last_error = exc
                time.sleep(self.backoff ** (attempt - 1) + random.random() * self.jitter)
            except Exception as exc:
                info = classify_exception(exc)
                if info.category == ErrorClass.SYSTEM:
                    raise ArchiveError(info.code, info.message, ErrorClass.SYSTEM) from exc
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
