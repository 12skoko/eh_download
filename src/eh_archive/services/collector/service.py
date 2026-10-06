from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ...config.loader import AppConfig, CrawlConfig, SecretsConfig
from ...db.repository import ArchiveRepository
from ...domain.errors import ArchiveError, ErrorClass
from ...domain.models import Manga
from ...domain.states import QueueSource, Status
from ...integrations.http import RoleSession
from ...logging import get_logger
from .archive import PageArchive
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


@dataclass(frozen=True)
class ParsedCollectionPage:
    """One validated listing page, with no persistence or scheduling effects."""

    url: str
    items: tuple[Manga, ...]
    next_url: str | None
    prev_url: str | None = None
    first_page: bool = False


def parse_collection_page(html: str, url: str) -> ParsedCollectionPage:
    """Malformed rows or navigation must never advance a persistent cursor."""
    from bs4 import BeautifulSoup

    def invalid(message: str) -> ArchiveError:
        return ArchiveError("collection_page_structure_invalid", message, ErrorClass.SYSTEM)

    def navigation_link(node: Any, direction: str) -> str:
        href = str(node.get("href", "")).strip()
        if not href:
            raise invalid(f"EH {direction}-page link is empty")
        resolved = urljoin(url, href)
        destination, origin = urlsplit(resolved), urlsplit(url)
        if (
            destination.scheme not in {"http", "https"}
            or destination.netloc.casefold() != origin.netloc.casefold()
            or destination.scheme != origin.scheme
            or destination.username is not None
            or destination.password is not None
            or destination.fragment
            or resolved == url
        ):
            raise invalid(f"EH {direction}-page link is invalid or leaves the listing origin")
        return resolved

    soup = BeautifulSoup(html, "lxml")
    next_link = soup.find("a", id="unext")
    terminal = soup.find("span", id="unext")
    if (next_link is None) == (terminal is None):
        raise invalid("EH listing must have one explicit next-page or terminal marker")
    next_url = navigation_link(next_link, "next") if next_link is not None else None
    previous_link = soup.find("a", id="uprev")
    first_marker = soup.find("span", id="uprev")
    if previous_link is not None and first_marker is not None:
        raise invalid("EH listing has contradictory previous-page markers")
    prev_url = navigation_link(previous_link, "previous") if previous_link is not None else None
    table = soup.select_one("table.itg.glte")
    items: list[Manga] = []
    seen: set[str] = set()
    if table is not None:
        for row in table.find_all("tr"):
            if row.find_parent("table") is not table:
                continue
            if row.find("th") is not None and row.find("td") is None:
                continue
            try:
                manga = parse_metadata(row)
            except ValueError as exc:
                raise invalid("an EH listing row failed metadata parsing") from exc
            if manga.manga_id in seen:
                raise invalid("EH listing contains duplicate gallery identifiers")
            seen.add(manga.manga_id)
            items.append(manga)
    if not items:
        text = soup.get_text(" ", strip=True).casefold()
        empty_marker = any(value in text for value in ("no hits found", "no galleries found"))
        if terminal is None or not empty_marker:
            raise invalid("empty EH listing lacks a confirmed no-results terminal marker")
    return ParsedCollectionPage(
        url=url, items=tuple(items), next_url=next_url,
        prev_url=prev_url, first_page=first_marker is not None,
    )


def fetch_collection_page(
    http_client: Any, url: str, *, role: str = "browse",
    timeout: float = 30.0, **request_options: Any,
) -> ParsedCollectionPage:
    """Fetch once; the caller owns retry, pacing, transactions and continuation."""
    html = http_client.get_text(url, role=role, timeout=timeout, **request_options)
    return parse_collection_page(html, url)


class Collector:
    def __init__(
        self,
        repository: ArchiveRepository,
        config: AppConfig,
        crawl: CrawlConfig,
        secrets: SecretsConfig,
        *,
        http_client: Any | None = None,
        run_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.crawl = crawl
        self.secrets = secrets
        self.http = http_client
        self._role_session: RoleSession | None = None
        self._archive = (
            PageArchive(crawl.collect_archive_dir, run_id=run_id)
            if crawl.collect_archive_enabled else None
        )
        if self._archive is not None:
            log.info(
                "collection page archive: run_id=%s directory=%s",
                self._archive.run_id, self._archive.directory,
            )

    @property
    def archive_dir(self) -> Path | None:
        return self._archive.directory if self._archive is not None else None

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
            status, defer_until, _ = collection_status(
                manga,
                self.crawl.observation_days,
            )
            manga.status = status
            manga.defer_until = defer_until
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
            if self._archive is not None:
                self._archive.save(html, url=current_url, source_url=url)
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


def manga_record(manga: Manga):
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


# Preserve the existing collector's conversion hook and external compatibility.
_record = manga_record
