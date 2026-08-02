from __future__ import annotations

from typing import Any


class Aria2Adapter:
    def __init__(self, **options: Any) -> None:
        try:
            import aria2p
        except ImportError as exc:
            raise RuntimeError("Install eh-archive[aria2] to use aria2") from exc
        self.api = aria2p.API(aria2p.Client(**options))

    def download(
        self, url: str, *, directory: str, filename: str, options: dict[str, Any] | None = None
    ) -> str:
        download = self.api.add_uris(
            [url], options={"dir": directory, "out": filename, **(options or {})}
        )
        return str(download.gid)

    def is_complete(self, gid: str) -> bool:
        download = self.api.get_download(gid)
        return bool(download and download.is_complete)

    def remove(self, gid: str) -> bool:
        download = self.api.get_download(gid)
        if download is None:
            return True
        remover = getattr(download, "remove", None)
        if remover is None:
            return False
        try:
            remover(force=True, files=True)
        except TypeError:
            remover()
        return True
