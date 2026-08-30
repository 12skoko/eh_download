from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ...config.loader import AppConfig, CrawlConfig, SecretsConfig
from ...db.repository import ArchiveRepository
from ...domain.errors import ArchiveError, ErrorClass
from ...domain.models import Manga
from ...domain.states import QueueSource, Status
from ...integrations.http import RoleSession
from ...logging import get_logger
from .parser import parse_metadata
from .timing import collection_status

log = get_logger(__name__)


@dataclass
class CollectionResult:
    discovered: int = 0
    deferred: int = 0
    errors: int = 0
    items: list[CollectedManga] = field(default_factory=list)
    pages: list[CollectedPage] = field(default_factory=list)

    def add(self, other: CollectionResult) -> None:
        self.discovered += other.discovered
        self.deferred += other.deferred
        self.errors += other.errors
        self.items.extend(other.items)
        self.pages.extend(other.pages)


@dataclass(frozen=True)
class CollectedManga:
    manga_id: str
    action: str
    name: str
    category: str
    status: str
    remark: str | None


@dataclass(frozen=True)
class CollectedPage:
    url: str
    discovered: int
    created: int
    updated: int
    deferred: int
    errors: int
    items: tuple[CollectedManga, ...]


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
        self._role_session: RoleSession | None = None

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
            raise ArchiveError(
                "collection_page_structure_invalid",
                "collection page has no EH gallery table",
                ErrorClass.SYSTEM,
            )
        result = CollectionResult()
        for row in table.find_all("tr", recursive=False):
            try:
                manga = parse_metadata(row)
            except ValueError:
                result.errors += 1
                continue
            result.discovered += 1
            manga.queue_source = QueueSource(source)
            status, defer_until, remark = collection_status(
                manga,
                self.crawl.observation_days,
            )
            manga.status = status
            manga.defer_until = defer_until
            manga.remark = remark
            incoming = _record(manga)
            stored = self.repository.upsert_manga(incoming, actor=actor)
            persisted = stored or incoming
            if persisted.status == Status.DEFERRED.value:
                result.deferred += 1
            result.items.append(
                CollectedManga(
                    manga_id=persisted.manga_id,
                    action="created" if stored is None or stored is incoming else "updated",
                    name=persisted.name,
                    category=persisted.category,
                    status=persisted.status,
                    remark=persisted.remark,
                )
            )
        if result.errors and result.discovered == 0:
            raise ArchiveError(
                "collection_page_structure_invalid",
                "all gallery rows failed EH metadata parsing",
                ErrorClass.SYSTEM,
            )
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
            page_result = self.collect_html(html, source=source, actor=actor)
            page_result.pages.append(
                CollectedPage(
                    url=current_url,
                    discovered=page_result.discovered,
                    created=sum(item.action == "created" for item in page_result.items),
                    updated=sum(item.action == "updated" for item in page_result.items),
                    deferred=page_result.deferred,
                    errors=page_result.errors,
                    items=tuple(page_result.items),
                )
            )
            result.add(page_result)
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
            if self._role_session is None:
                self._role_session = RoleSession(self.config, self.secrets)
            return self._role_session.get_text(url, role="browse", timeout=timeout)
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
        screen_group_id=manga.screen_group_id,
        priority=manga.priority,
        defer_until=manga.defer_until,
        source_fetched_at=datetime.now(UTC),
    )
