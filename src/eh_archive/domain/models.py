from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .states import DownloadMethod, QueueSource, Status


@dataclass
class Manga:
    manga_id: str
    name: str
    link: str
    real_name: str = ""
    torrent_link: str = ""
    posted_at: datetime | None = None
    category: str = ""
    tags_raw: str = ""
    pages: int | None = None
    rating: int | None = None
    uploader: str = ""
    remark: str | None = None
    queue_source: QueueSource = QueueSource.AUTOMATIC
    status: Status = Status.DISCOVERED
    screen_group_id: str | None = None
    priority: int = 0
    download_method: DownloadMethod | None = None
    defer_until: datetime | None = None
    artifact_generation: int | None = None
    artifact_location: str | None = None
    artifact_filename: str | None = None
    rename_target_filename: str | None = None
    artifact_kind: str | None = None
    artifact_size: int | None = None
    artifact_sha1: str | None = None
    lrr_archive_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MangaInfo:
    manga_id: str
    name: str = ""
    roman_name: str = ""
    real_name: str = ""
    link: str = ""
    category: str = ""
    uploader: str = ""
    posted_at: datetime | None = None
    language: str = ""
    estimated_size_raw: str = ""
    pages: int | None = None
    favorited: int | None = None
    rating_count: int | None = None
    rating: int | None = None
    fetched_at: datetime | None = None
    tags_raw: str = ""
    tags_translated_raw: str = ""
    archive_url: str | None = None
    parent_id: str | None = None

    def is_complete(self) -> bool:
        required_text = (
            self.name,
            self.link,
            self.category,
            self.uploader,
            self.language,
            self.estimated_size_raw,
        )
        return (
            all(value is not None and value != "" for value in required_text)
            and self.posted_at is not None
            and self.pages is not None
            and self.tags_raw is not None
        )


@dataclass(frozen=True)
class MangaSummary:
    manga_id: str
    name: str
    status: Status
    priority: int
    download_method: DownloadMethod | None
    last_error_code: str | None = None
