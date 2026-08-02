from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ...db.models import MangaRecord
from ...db.repository import ArchiveRepository, utcnow
from ...domain.states import QueueSource, Status
from ..collector.parser import get_real_name


@dataclass(frozen=True)
class PicacgEntry:
    cid: str
    name: str
    real_name: str
    author: str
    classification: str
    favorited: str
    link: str


def parse_export_page(html: str, cids: Iterable[str], *, base_url: str) -> list[PicacgEntry]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("beautifulsoup4 is required for Picacg import") from exc
    soup = BeautifulSoup(html, "lxml")
    items = soup.find_all("li", class_="cat-item")
    cid_list = list(cids)
    if len(items) != len(cid_list):
        raise ValueError(f"cid count {len(cid_list)} does not match item count {len(items)}")
    result: list[PicacgEntry] = []
    for item, cid in zip(items, cid_list):
        title_node = item.select_one("div.comic-title")
        author_node = item.select_one("div.comic-author span.c-author")
        category_node = item.select_one("div.c-list-cat span.c-cat")
        score_node = item.select_one("span.c-score")
        if not all((title_node, author_node, category_node, score_node)):
            raise ValueError("Picacg item is missing title, author, category or score")
        name = title_node.get_text(" ", strip=True).replace("(完)", "").strip()
        result.append(
            PicacgEntry(
                str(cid),
                name,
                get_real_name(name),
                author_node.get_text(strip=True),
                category_node.get_text(strip=True),
                score_node.get_text(strip=True),
                base_url.rstrip("/") + "/" + str(cid),
            )
        )
    return result


class PicacgService:
    def __init__(self, repository: ArchiveRepository, *, base_url: str) -> None:
        self.repository = repository
        self.base_url = base_url

    def import_entries(self, entries: Iterable[PicacgEntry], *, actor: str = "picacg") -> int:
        count = 0
        for entry in entries:
            manga_id = f"picacg/{entry.cid}"
            record = MangaRecord(
                manga_id=manga_id,
                name=entry.name,
                real_name=entry.real_name,
                link=entry.link,
                category="Manga",
                uploader=entry.author,
                tags_raw=f"picacg:{entry.classification}",
                remark=f"favorited:{entry.favorited}",
                queue_source=QueueSource.AUTOMATIC.value,
                status=Status.DISCOVERED.value,
                source_fetched_at=datetime.now(UTC),
            )
            self.repository.upsert_manga(record, actor=actor)
            count += 1
        return count

    def screen_entries(self, *, actor: str = "picacg") -> int:
        """Move Picacg records into the normal queue when no EH title exists."""
        from sqlalchemy import select

        rows = list(
            self.repository.session.scalars(
                select(MangaRecord).where(
                    MangaRecord.manga_id.like("picacg/%"),
                    MangaRecord.status == Status.DISCOVERED.value,
                )
            )
        )
        existing_names = {
            row.real_name
            for row in self.repository.session.scalars(
                select(MangaRecord).where(~MangaRecord.manga_id.like("picacg/%"))
            )
        }
        changed = 0
        for row in rows:
            row.status = (
                Status.DOWNLOAD_PENDING.value
                if row.real_name not in existing_names
                else Status.SKIPPED.value
            )
            row.queue_source = (
                QueueSource.MANUAL.value
                if row.status == Status.DOWNLOAD_PENDING.value
                else QueueSource.AUTOMATIC.value
            )
            row.status_updated_at = row.updated_at = utcnow()
            row.row_version += 1
            changed += 1
        return changed


def read_export_directory(root: str | Path, *, base_url: str) -> list[PicacgEntry]:
    root = Path(root)
    entries: list[PicacgEntry] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        cids = []
        for line in (directory / "cid.txt").read_text(encoding="utf-8").splitlines():
            match = re.search(r'\d+:\s*"([\da-f]+)"', line)
            if match:
                cids.append(match.group(1))
        entries.extend(
            parse_export_page(
                (directory / "index.html").read_text(encoding="utf-8"), cids, base_url=base_url
            )
        )
    return entries
