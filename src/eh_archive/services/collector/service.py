from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ...config.loader import AppConfig, CrawlConfig, SecretsConfig
from ...db.repository import ArchiveRepository
from ...domain.models import Manga
from ...domain.states import QueueSource, Status
from ...integrations.http import RoleSession
from ...logging import get_logger
from .parser import judge_screen_flag, observation_deadline, parse_metadata

log = get_logger(__name__)


@dataclass
class CollectionResult:
    discovered: int = 0
    queued: int = 0
    deferred: int = 0
    skipped: int = 0
    unavailable: int = 0
    errors: int = 0

    def add(self, other: CollectionResult) -> None:
        self.discovered += other.discovered
        self.queued += other.queued
        self.deferred += other.deferred
        self.skipped += other.skipped
        self.unavailable += other.unavailable
        self.errors += other.errors


class Collector:
    def __init__(
        self,
        repository: ArchiveRepository,
        config: AppConfig,
        crawl: CrawlConfig,
        secrets: SecretsConfig,
        *,
        http_client: Any | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.crawl = crawl
        self.secrets = secrets
        self.http = http_client

    def collect_html(
        self, html: str, *, source: str = QueueSource.AUTOMATIC.value, actor: str = "collector"
    ) -> CollectionResult:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:
            raise RuntimeError("beautifulsoup4 is required for collection") from exc
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table", class_="itg glte")
        if table is None:
            raise ValueError("collection page has no gallery table")
        result = CollectionResult()
        for row in table.find_all("tr", recursive=False):
            try:
                manga = parse_metadata(row)
            except ValueError:
                result.errors += 1
                continue
            result.discovered += 1
            manga.queue_source = QueueSource(source)
            flag = judge_screen_flag(
                manga,
                self.crawl.name_keywords,
                self.crawl.tag_keywords,
                observation_days=self.crawl.observation_days,
            )
            if manga.category in self.crawl.exclude_categories or flag == 0:
                manga.status = Status.SKIPPED
                manga.remark = (
                    "excluded_category"
                    if manga.category in self.crawl.exclude_categories
                    else "screen_rejected"
                )
                result.skipped += 1
            elif flag == -1 and manga.posted_at is None:
                manga.status = Status.MANUAL_REVIEW
                manga.remark = "missing_posted_at"
                result.errors += 1
            elif flag == -1:
                manga.status = Status.DEFERRED
                manga.remark = "observation_period"
                manga.defer_until = observation_deadline(
                    manga.posted_at, self.crawl.observation_days
                )
                result.deferred += 1
            else:
                manga.status = Status.DOWNLOAD_PENDING
                result.queued += 1
            self.repository.upsert_manga(_record(manga), actor=actor)
        return result

    def collect_url(
        self,
        url: str,
        *,
        source: str = QueueSource.AUTOMATIC.value,
        actor: str = "collector",
        timeout: float = 30.0,
        follow_next: bool = True,
        end: int | None = None,
    ) -> CollectionResult:
        result = CollectionResult()
        current_url = url
        seen: set[str] = set()
        while True:
            if current_url in seen:
                raise RuntimeError(f"collection pagination loop detected: {current_url}")
            seen.add(current_url)
            html = self._get_page(current_url, timeout=timeout)
            result.add(self.collect_html(html, source=source, actor=actor))
            if not follow_next:
                break
            next_url = self._next_url(html, current_url)
            if not next_url:
                break
            if end is not None and self._next_number(next_url) <= end:
                break
            current_url = next_url
        return result

    def _get_page(self, url: str, *, timeout: float) -> str:
        if self.http is None:
            return RoleSession(self.config, self.secrets).get_text(
                url, role="browse", timeout=timeout
            )
        return self.http.get_text(url, role="browse", timeout=timeout)

    @staticmethod
    def _next_url(html: str, current_url: str) -> str | None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        next_link = soup.find("a", id="unext")
        href = next_link.get("href") if next_link else None
        return urljoin(current_url, str(href)) if href else None

    @staticmethod
    def _next_number(next_url: str) -> int:
        values = parse_qs(urlsplit(next_url).query).get("next")
        if not values:
            raise ValueError(f"next page URL has no next parameter: {next_url}")
        try:
            return int(values[0])
        except ValueError as exc:
            raise ValueError(f"next page URL has an invalid next parameter: {next_url}") from exc


def _record(manga: Manga):
    from ...db.models import MangaRecord

    return MangaRecord(
        manga_id=manga.manga_id,
        name=manga.name,
        real_name=manga.real_name,
        link=manga.link,
        torrent_link=manga.torrent_link,
        posted_at=manga.posted_at,
        category=manga.category,
        tags_raw=manga.tags_raw,
        pages=manga.pages,
        rating=manga.rating,
        uploader=manga.uploader,
        remark=manga.remark,
        queue_source=manga.queue_source.value,
        status=manga.status.value,
        priority=manga.priority,
        defer_until=manga.defer_until,
        source_fetched_at=datetime.now(UTC),
    )
