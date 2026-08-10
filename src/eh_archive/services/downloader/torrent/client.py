from __future__ import annotations

from pathlib import Path
from typing import Any

from ....domain.errors import ArchiveError, ErrorClass

QBITTORRENT_CATEGORY = "eharchive"


def torrent_category(info: Any) -> str:
    value = getattr(info, "category", None)
    if value is None:
        try:
            value = info["category"]
        except (KeyError, TypeError):
            value = ""
    return str(value or "")


def is_managed_torrent(info: Any) -> bool:
    return torrent_category(info) == QBITTORRENT_CATEGORY


class QBittorrentClient:
    """Thin qBittorrent adapter; business state remains in EH Archive."""

    def __init__(self, **options: Any) -> None:
        try:
            import qbittorrentapi
        except ImportError as exc:
            raise RuntimeError("qbittorrent-api is missing; reinstall eh-archive") from exc
        self._system_exceptions = (
            qbittorrentapi.exceptions.APIConnectionError,
            qbittorrentapi.exceptions.LoginFailed,
            qbittorrentapi.exceptions.Unauthorized401Error,
            qbittorrentapi.exceptions.Forbidden403Error,
            qbittorrentapi.exceptions.UnsupportedQbittorrentVersion,
        )
        try:
            self.client = qbittorrentapi.Client(**options)
            self.client.auth_log_in()
        except getattr(self, "_system_exceptions", ()) as exc:
            raise ArchiveError(
                "qbittorrent_unavailable",
                f"qBittorrent is unavailable or rejected authentication: {exc}",
                ErrorClass.SYSTEM,
            ) from exc

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except getattr(self, "_system_exceptions", ()) as exc:
            raise ArchiveError(
                "qbittorrent_unavailable",
                f"qBittorrent is unavailable or rejected authentication: {exc}",
                ErrorClass.SYSTEM,
            ) from exc

    def add(
        self, torrent_bytes: bytes, *, save_path: str | Path, display_name: str | None = None
    ) -> str:
        options: dict[str, Any] = {
            "torrent_files": torrent_bytes,
            "save_path": str(save_path),
            "category": QBITTORRENT_CATEGORY,
        }
        if display_name is not None:
            options["rename"] = display_name
        self._call(
            "torrents_add",
            **options,
        )
        # qBittorrent may acknowledge before the hash is visible. Polling is
        # bounded and does not hold a database lease.
        for _ in range(10):
            for torrent in self._call("torrents_info"):
                if _path_key(torrent.save_path) == _path_key(save_path) and is_managed_torrent(
                    torrent
                ):
                    return str(torrent.hash)
            import time

            time.sleep(0.5)
        raise RuntimeError("qBittorrent accepted torrent but returned no stable hash")

    def info(self, torrent_hash: str) -> Any | None:
        values = self._call("torrents_info", torrent_hashes=torrent_hash)
        return values[0] if values else None

    def delete(self, torrent_hash: str, *, delete_files: bool = False) -> None:
        self._call("torrents_delete", torrent_hashes=torrent_hash, delete_files=delete_files)


def _path_key(value: str | Path) -> str:
    return str(value).replace("\\", "/").rstrip("/").casefold()
