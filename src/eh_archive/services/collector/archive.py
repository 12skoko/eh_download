from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ...logging import get_logger

log = get_logger(__name__)


class PageArchive:
    """Keep received HTML outside the collection's database transaction."""

    def __init__(self, root: Path, *, run_id: str | None = None) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        started = datetime.now(UTC)
        self.started_at = started.isoformat()
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", self.run_id)
        self.directory = (
            root.expanduser().resolve() / f"{started:%Y%m%d_%H%M%S_%f}_{safe_id}"
        )
        self._initialized = False
        self._sequence = 0
        self._pages: list[dict[str, str | int]] = []

    def save(self, html: str, *, url: str, source_url: str) -> None:
        self._sequence += 1
        fetched_at = datetime.now(UTC).isoformat()
        filename = f"{self._sequence:06d}.html.gz"
        temporary_html = self.directory / f".{filename}.tmp"
        temporary_manifest = self.directory / ".manifest.json.tmp"
        stage = "html"
        try:
            if not self._initialized:
                self.directory.parent.mkdir(parents=True, exist_ok=True)
                # Never reuse a directory belonging to another collector.
                self.directory.mkdir(exist_ok=False)
                self._initialized = True
            with gzip.open(temporary_html, "wb", compresslevel=6) as handle:
                handle.write(html.encode("utf-8"))
            temporary_html.replace(self.directory / filename)
            self._pages.append({
                "file": filename,
                "url": url,
                "source_url": source_url,
                "fetched_at": fetched_at,
                "page_number": self._sequence,
            })
            stage = "manifest"
            # Keep successfully saved pages in memory if publication fails;
            # the next save retries the complete manifest, including this page.
            manifest = {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "pages": self._pages,
            }
            temporary_manifest.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_manifest.replace(self.directory / "manifest.json")
        except (OSError, UnicodeError):
            log.warning(
                "collection page archive failed: stage=%s run_id=%s page=%s directory=%s",
                stage, self.run_id, self._sequence, self.directory, exc_info=True,
            )
        finally:
            # A failed attempt must not leave a truncated file with a final name.
            if self._initialized:
                for temporary in (temporary_html, temporary_manifest):
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        log.warning("failed to remove archive temporary file: %s", temporary)


class BatchPageArchive:
    """Resumable job archive. The caller must hold a valid claim while saving."""

    def __init__(
        self, root: Path, *, workflow_id: int, job_id: int, workflow_created_at: str,
    ) -> None:
        self.directory = root / str(workflow_id) / str(job_id)
        self._identity = {
            "workflow_id": workflow_id,
            "job_id": job_id,
            "workflow_created_at": workflow_created_at,
        }
        self._pending: dict[int, dict] = {}

    def _manifest(self) -> dict:
        path = self.directory / "manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {**self._identity, "pages": []}
        if not isinstance(manifest, dict) or any(
            manifest.get(key) != value for key, value in self._identity.items()
        ):
            raise ValueError("archive manifest belongs to a different workflow or job")
        pages = manifest.get("pages")
        if not isinstance(pages, list):
            raise TypeError("archive manifest has invalid pages")
        seen = set()
        for entry in pages:
            if not isinstance(entry, dict):
                raise TypeError("archive manifest has an invalid entry")
            number = entry.get("request_number")
            if (
                type(number) is not int or number <= 0 or number in seen
                or entry.get("file") != f"{number:06d}.html.gz"
                or not all(isinstance(entry.get(key), str)
                           for key in ("url", "source_url", "fetched_at"))
            ):
                raise ValueError("archive manifest has an invalid request")
            seen.add(number)
        return manifest

    def save(
        self, compressed_html: bytes, *, request_number: int, url: str,
        source_url: str, fetched_at: str,
    ) -> bool:
        filename = f"{request_number:06d}.html.gz"
        temporary_html = temporary_manifest = None
        stage = "html"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{filename}.", suffix=".tmp", dir=self.directory,
            )
            temporary_html = Path(raw_path)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(compressed_html)
                handle.flush()
                os.fsync(handle.fileno())
            destination = self.directory / filename
            # Publish a complete file without ever replacing an earlier response.
            if os.name == "nt":
                os.rename(temporary_html, destination)
            else:
                os.link(temporary_html, destination)
            self._pending[request_number] = {
                "file": filename, "url": url, "source_url": source_url,
                "fetched_at": fetched_at, "request_number": request_number,
            }
            stage = "manifest"
            manifest = self._manifest()
            entries = {entry["request_number"]: entry for entry in manifest["pages"]}
            for number, entry in self._pending.items():
                if number in entries and entries[number] != entry:
                    raise ValueError("archive manifest request number conflict")
                entries[number] = entry
            manifest["pages"] = [entries[number] for number in sorted(entries)]
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".manifest.", suffix=".tmp", dir=self.directory,
            )
            temporary_manifest = Path(raw_path)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
                handle.flush()
                os.fsync(handle.fileno())
            temporary_manifest.replace(self.directory / "manifest.json")
            self._pending.clear()
            return True
        except (OSError, UnicodeError, ValueError, TypeError):
            log.warning(
                "full collection page archive failed: stage=%s workflow=%s job=%s request=%s",
                stage, self._identity["workflow_id"], self._identity["job_id"], request_number,
                exc_info=True,
            )
            return False
        finally:
            for temporary in (temporary_html, temporary_manifest):
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        log.warning("failed to remove archive temporary file: %s", temporary)
