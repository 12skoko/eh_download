from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.errors import ArchiveError, ErrorClass


class HAHDownloader:
    """Adapter for the EH H@H queue and its configured download directory."""

    def __init__(
        self,
        *,
        session: Any,
        root: str | Path,
        cookies: dict[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self.session, self.root = session, Path(root)
        self.cookies, self.proxies = cookies or {}, proxies

    def queue(self, archive_url: str, *, resolution: str = "org") -> None:
        response = self.session.post(
            archive_url.replace("--", "-"),
            data={"hathdl_xres": resolution},
            cookies=self.cookies,
            proxies=self.proxies,
            timeout=30,
        )
        response.raise_for_status()
        if "queued for client" not in response.text:
            raise ArchiveError(
                "hah_queue_rejected", "H@H did not queue the archive", ErrorClass.ITEM
            )

    def find_completed(self, manga_id: str) -> Path | None:
        prefix = f"[{manga_id.split('/', 1)[0]}]"
        if not self.root.exists():
            return None
        for candidate in self.root.iterdir():
            if (
                candidate.name.startswith(prefix)
                and not candidate.is_symlink()
                and (candidate / "galleryinfo.txt").is_file()
            ):
                return candidate
        return None
