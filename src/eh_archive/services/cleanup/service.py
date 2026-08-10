from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ...domain.errors import ArchiveError, ErrorClass
from ..downloader.torrent import is_managed_torrent


class CleanupService:
    """Perform only explicitly registered, idempotent cleanup operations."""

    def __init__(
        self, *, qbit: Any | None = None, aria2: Any | None = None, lanraragi: Any | None = None
    ) -> None:
        self.qbit, self.aria2, self.lanraragi = qbit, aria2, lanraragi

    def remove_local(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return True
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return not path.exists()

    def remove_torrent(self, torrent_hash: str, *, delete_files: bool = False) -> bool:
        if self.qbit is None:
            return False
        try:
            info = self.qbit.info(torrent_hash)
            if info is None:
                return True
            if not is_managed_torrent(info):
                return True
            self.qbit.delete(torrent_hash, delete_files=delete_files)
            return True
        except ArchiveError as exc:
            if exc.info.category == ErrorClass.SYSTEM:
                raise
            return False
        except Exception:  # noqa: BLE001 - qBittorrent adapters use their own exception hierarchy
            return False

    def remove_archive(self, archive_id: str) -> str:
        if self.lanraragi is None:
            return "review"
        outcome = self.lanraragi.delete(archive_id)
        return outcome.kind
