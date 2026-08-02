from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from ...domain.models import Manga, MangaInfo


def get_real_name(name: str) -> str:
    """Remove balanced prefix/suffix markers used by EH titles."""
    if not name:
        return ""
    left = 0
    right = len(name)
    paren = bracket = 0
    for index, char in enumerate(name):
        if paren == 0 and bracket == 0 and char not in "([ ":
            left = index
            break
        if char == "(":
            paren += 1
        elif char == "[":
            bracket += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "]":
            bracket = max(0, bracket - 1)
    paren = bracket = 0
    for index in range(len(name) - 1, -1, -1):
        char = name[index]
        if paren == 0 and bracket == 0 and char not in ")] ":
            right = index + 1
            break
        if char == ")":
            paren += 1
        elif char == "]":
            bracket += 1
        elif char == "(":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket = max(0, bracket - 1)
    return name[left:right]


def cal_rating(background_x: str | int, background_y: str | int) -> int:
    rating = (5 - int(background_x) // 16) * 10
    if str(background_y) == "21":
        rating -= 5
    return rating


def parse_tag_table(tag_soup: Any) -> str:
    if tag_soup is None:
        return ""
    values: list[str] = []
    for row in tag_soup.find_all("tr", recursive=False):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        values.append(
            cells[0].get_text(strip=True)
            + ",".join(
                x.get_text(strip=True) for x in cells[1].find_all(["div", "a"], recursive=False)
            )
        )
    return ",".join(values)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def _int_text(value: str, default: int | None = None) -> int | None:
    found = re.search(r"-?\d+", value or "")
    return int(found.group()) if found else default


def parse_metadata(tr_soup: Any) -> Manga:
    extended = tr_soup.find("td", class_="gl2e")
    if extended is None:
        raise ValueError("list row is missing gl2e metadata")
    div = extended.find("div")
    metadata = div.find("div", class_="gl3e") if div else None
    title = metadata.find_next_sibling("a") if metadata else None
    if title is None or not title.get("href"):
        raise ValueError("list row is missing gallery link")
    link = str(title["href"])
    match = re.search(r"(?:exhentai|e-hentai)\.org/g/(\d+/[\w-]+)/?", link)
    if not match:
        match = re.search(r"/g/(\d+/[\w-]+)/?", link)
    if not match:
        raise ValueError(f"invalid gallery link: {link}")
    manga_id = match.group(1)
    name_node = title.find("div", class_="glink")
    name = name_node.get_text(" ", strip=True) if name_node else title.get_text(" ", strip=True)
    metadata_divs = metadata.find_all("div", recursive=False) if metadata else []
    category = metadata_divs[0].get_text(strip=True) if len(metadata_divs) > 0 else ""
    posted_text = metadata_divs[1].get_text(strip=True) if len(metadata_divs) > 1 else ""
    rating = 0
    if len(metadata_divs) > 2:
        match_rating = re.search(
            r"background-position:\s*(-?\d+)px\s+(-?\d+)px", metadata_divs[2].get("style", "")
        )
        if match_rating:
            rating = cal_rating(match_rating.group(1), match_rating.group(2))
    uploader = metadata_divs[3].get_text(strip=True) if len(metadata_divs) > 3 else ""
    pages = (
        _int_text(metadata_divs[4].get_text(" ", strip=True)) if len(metadata_divs) > 4 else None
    )
    torrent_link = ""
    if len(metadata_divs) > 5 and metadata_divs[5].find("a"):
        torrent_link = str(metadata_divs[5].find("a").get("href", ""))
    tag_node = name_node.find_next_sibling("div") if name_node else None
    posted_at = _parse_datetime(posted_text)
    return Manga(
        manga_id=manga_id,
        name=html.unescape(name),
        real_name=get_real_name(name),
        link=link,
        torrent_link=torrent_link,
        posted_at=posted_at,
        category=category,
        tags_raw=parse_tag_table(tag_node.find("table") if tag_node else None),
        pages=pages,
        rating=rating,
        uploader=uploader,
    )


def parse_info(
    soup: Any, tag_translation: EhTagTranslation | None = None
) -> tuple[MangaInfo, str, str | None]:
    def text(selector: str, default: str = "") -> str:
        node = soup.select_one(selector)
        return node.get_text(" ", strip=True) if node else default

    name = html.unescape(text("#gj")).replace('"', '""')
    roman_name = html.unescape(text("#gn")).replace('"', '""')
    if not name:
        name, roman_name = roman_name, ""
    details = [node.get_text(" ", strip=True) for node in soup.select("td.gdt2")]
    category = text("#gdc")
    uploader = text("#gdn")
    posted_at = _parse_datetime(details[0] if len(details) > 0 else None)
    language = (details[3] if len(details) > 3 else "").replace("\xa0", "")
    pages = _int_text(details[5] if len(details) > 5 else "")
    favorited_text = details[6] if len(details) > 6 else ""
    favorited = _int_text(favorited_text, 1 if favorited_text.lower() == "once" else 0)
    rating_count = _int_text(text("#rating_count"), 0)
    average = re.search(r"-?\d+(?:\.\d+)?", text("#rating_label"))
    rating = round(float(average.group()) * 100) if average else None
    tags: list[str] = []
    row = ""
    for raw in soup.select_one("#taglist").stripped_strings if soup.select_one("#taglist") else ():
        if ":" in raw:
            row = raw
        elif row:
            tags.append(row + raw)
    tags_raw = ",".join(tags)
    tags_translated = tag_translation.get_trans(tags_raw) if tag_translation else tags_raw
    archive_url = None
    archive = soup.find("a", string=lambda value: value and "Archive Download" in value)
    if archive and archive.get("onclick"):
        match = re.search(r"popUp\('(https://exhentai\.org/archiver\.php.*?)',", archive["onclick"])
        archive_url = match.group(1) if match else None
    parent = details[1] if len(details) > 1 and details[1] != "None" else None
    info = MangaInfo(
        manga_id="",
        name=name,
        roman_name=roman_name,
        real_name=get_real_name(name),
        link="",
        category=category,
        uploader=uploader,
        posted_at=posted_at,
        language=language,
        estimated_size_raw=details[4] if len(details) > 4 else "",
        pages=pages,
        favorited=favorited,
        rating_count=rating_count,
        rating=rating,
        tags_raw=tags_raw,
        tags_translated_raw=tags_translated,
        archive_url=archive_url,
        parent_id=parent,
    )
    return info, archive_url or "", parent


class EhTagTranslation:
    rows: ClassVar[dict[str, int]] = {
        "rows": 0,
        "reclass": 1,
        "language": 2,
        "parody": 3,
        "character": 4,
        "group": 5,
        "artist": 6,
        "cosplayer": 7,
        "male": 8,
        "female": 9,
        "mixed": 10,
        "other": 11,
    }

    def __init__(self, path: str | Path | None = None, data: dict[str, Any] | None = None) -> None:
        if data is None and path:
            with Path(path).open(encoding="utf-8") as handle:
                data = json.load(handle)
        self.data = data or {"data": []}
        groups = self.data.get("data", [])
        self.group_names = {
            str(k): v.get("name", str(k))
            for k, v in (groups[0].get("data", {}) if groups else {}).items()
        }
        self.values: dict[int, dict[str, str]] = {}
        for index, group in enumerate(groups):
            self.values[index] = {
                str(k): v.get("name", str(k)) for k, v in group.get("data", {}).items()
            }

    def get_trans(self, value: str) -> str:
        result: list[str] = []
        for raw in value.split(","):
            if not raw or ":" not in raw:
                continue
            row, tag = raw.split(":", 1)
            try:
                row_index = self.rows[row]
            except KeyError:
                result.append(raw)
                continue
            result.append(
                f"{self.group_names.get(row, row)}:{self.values.get(row_index, {}).get(tag, tag)}"
            )
        return ",".join(result)


def contains_key(text: str, keyword: str) -> bool:
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text))


def judge_screen_flag(
    manga: Manga,
    name_keywords: tuple[str, ...] | list[str] = (),
    tag_keywords: tuple[str, ...] | list[str] = (),
    *,
    observation_days: int = 1,
    now: datetime | None = None,
) -> int:
    languages = {
        "english",
        "korean",
        "russian",
        "french",
        "dutch",
        "hungarian",
        "italian",
        "polish",
        "portuguese",
        "spanish",
        "thai",
        "vietnamese",
        "ukrainian",
    }
    lowered_tags = manga.tags_raw.lower()
    if (
        "translated" in lowered_tags
        and "chinese" not in lowered_tags
        and any(language in lowered_tags for language in languages)
    ):
        return 0
    if any(contains_key(manga.name.lower(), keyword.lower()) for keyword in name_keywords):
        return 2
    if any(contains_key(manga.tags_raw.lower(), keyword.lower()) for keyword in tag_keywords):
        return 2
    if manga.category in {"Manga", "Doujinshi"} and (
        "chinese" in lowered_tags or (manga.rating or 0) >= 30
    ):
        current = now or datetime.now(UTC)
        age = (current - manga.posted_at).total_seconds() if manga.posted_at else 0
        return 1 if age > observation_days * 86400 else -1
    return 0
