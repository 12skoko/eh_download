from __future__ import annotations

import gzip
import json
import re
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
