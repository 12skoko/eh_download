from __future__ import annotations

from typing import Any

from ...services.downloader.hah import HAHDownloader


class HAHAdapter:
    """Optional H@H client contract; installations can provide a concrete adapter."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def queue(self, archive_url: str, *, resolution: str = "org") -> str:
        return str(self.client.queue(archive_url, resolution=resolution))


__all__ = ["HAHAdapter", "HAHDownloader"]
