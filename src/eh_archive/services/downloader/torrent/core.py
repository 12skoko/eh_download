from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ....domain.errors import ArchiveError, ErrorClass
from ...paths import safe_filename


@dataclass(frozen=True)
class TorrentChoice:
    url: str
    size: str
    size_bytes: int
    seeds: int
    posted_at: datetime
    label: str
    page_order: int


_SIZE_UNITS = {
    "b": Decimal(1),
    "kib": Decimal(1024),
    "mib": Decimal(1024**2),
    "gib": Decimal(1024**3),
    "tib": Decimal(1024**4),
    "kb": Decimal(1000),
    "mb": Decimal(1000**2),
    "gb": Decimal(1000**3),
    "tb": Decimal(1000**4),
}


def _parse_size(value: str, *, field: str) -> int:
    parts = value.replace(",", "").split()
    if len(parts) != 2:
        raise ArchiveError(
            "invalid_torrent_size" if field == "torrent" else "invalid_estimated_size",
            f"cannot parse {field} size: {value!r}",
            ErrorClass.ITEM,
        )
    try:
        number = Decimal(parts[0])
        multiplier = _SIZE_UNITS[parts[1].casefold()]
    except (InvalidOperation, KeyError) as exc:
        raise ArchiveError(
            "invalid_torrent_size" if field == "torrent" else "invalid_estimated_size",
            f"cannot parse {field} size: {value!r}",
            ErrorClass.ITEM,
        ) from exc
    if not number.is_finite() or number <= 0:
        raise ArchiveError(
            "invalid_torrent_size" if field == "torrent" else "invalid_estimated_size",
            f"{field} size must be positive: {value!r}",
            ErrorClass.ITEM,
        )
    return int(number * multiplier)


def _field_text(form: Any, name: str) -> tuple[str, Any]:
    expected = f"{name}:".casefold()
    marker = next(
        (
            span
            for span in form.find_all("span")
            if span.get_text(" ", strip=True).casefold() == expected
        ),
        None,
    )
    cell = marker.find_parent(("td", "th")) if marker is not None else None
    if marker is None or cell is None:
        raise ArchiveError(
            "torrent_list_parse_error",
            f"torrent row is missing {name}",
            ErrorClass.ITEM,
        )
    label = marker.get_text(" ", strip=True)
    text = cell.get_text(" ", strip=True)
    value = text[len(label) :].strip() if text.startswith(label) else ""
    if not value:
        raise ArchiveError(
            "torrent_list_parse_error",
            f"torrent row has an empty {name}",
            ErrorClass.ITEM,
        )
    return value, cell


def _download_url(anchor: Any) -> str:
    onclick = str(anchor.get("onclick", ""))
    assignment = "document.location="
    start = onclick.find(assignment)
    if start >= 0:
        value = onclick[start + len(assignment) :].lstrip()
        if value and value[0] in {"'", '"'}:
            quote = value[0]
            end = value.find(quote, 1)
            if end > 1:
                return value[1:end]
    return str(anchor.get("href", "")).strip()


def _parse_torrent_form(form: Any, page_order: int) -> tuple[TorrentChoice | None, bool]:
    posted_raw, posted_cell = _field_text(form, "Posted")
    outdated = any(
        "color:red" in str(span.get("style", "")).replace(" ", "").casefold()
        for span in posted_cell.find_all("span")
    )
    if outdated:
        return None, True
    size_raw, _ = _field_text(form, "Size")
    seeds_raw, _ = _field_text(form, "Seeds")
    anchor = next(
        (
            item
            for item in form.find_all("a", href=True)
            if ".torrent" in str(item.get("href", ""))
            or "document.location=" in str(item.get("onclick", ""))
        ),
        None,
    )
    if anchor is None:
        raise ArchiveError(
            "torrent_list_parse_error",
            "torrent row has no download link",
            ErrorClass.ITEM,
        )
    url = _download_url(anchor)
    label = anchor.get_text(" ", strip=True)
    try:
        posted_at = datetime.strptime(posted_raw, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        seeds = int(seeds_raw)
    except ValueError as exc:
        raise ArchiveError(
            "torrent_list_parse_error",
            f"torrent row has invalid time or seed count: {posted_raw!r}, {seeds_raw!r}",
            ErrorClass.ITEM,
        ) from exc
    if not url or not label or seeds < 0:
        raise ArchiveError(
            "torrent_list_parse_error",
            "torrent row has an invalid URL, title, or seed count",
            ErrorClass.ITEM,
        )
    return (
        TorrentChoice(
            url=url,
            size=size_raw,
            size_bytes=_parse_size(size_raw, field="torrent"),
            seeds=seeds,
            posted_at=posted_at,
            label=label,
            page_order=page_order,
        ),
        outdated,
    )


def select_torrent(
    html: str,
    *,
    estimated_size_raw: str,
    skip_video: bool = False,
    excluded_resolutions: tuple[str, ...] = ("1280x", "800x", "1920x", "2560x"),
    video_markers: tuple[str, ...] = ("mp4", "video"),
) -> TorrentChoice:
    soup = BeautifulSoup(html, "lxml")
    active: list[TorrentChoice] = []
    torrent_forms = outdated_forms = 0
    outdated_section = False
    for node in soup.find_all(["p", "form"]):
        if node.name == "p":
            if node.get_text(" ", strip=True).casefold() == "outdated torrents:":
                outdated_section = True
            continue
        if node.find("input", attrs={"name": "gtid"}) is None:
            continue
        torrent_forms += 1
        if outdated_section:
            outdated_forms += 1
            continue
        choice, red_date = _parse_torrent_form(node, torrent_forms - 1)
        if red_date:
            outdated_forms += 1
            continue
        if choice is None:
            raise ArchiveError(
                "torrent_list_parse_error",
                "active torrent row could not be parsed",
                ErrorClass.ITEM,
            )
        active.append(choice)
    if not active:
        if torrent_forms and torrent_forms == outdated_forms:
            raise ArchiveError(
                "only_outdated_torrents",
                "gallery only has outdated torrents",
                ErrorClass.ITEM,
            )
        raise ArchiveError(
            "torrent_list_parse_error",
            "torrent page contains no recognizable active torrent rows",
            ErrorClass.ITEM,
        )

    if not skip_video:
        normalized_video_markers = tuple(value.casefold() for value in video_markers)
        if any(
            marker in choice.label.casefold()
            for choice in active
            for marker in normalized_video_markers
        ):
            raise ArchiveError(
                "video_torrent", "torrent list contains a video archive", ErrorClass.ITEM
            )

    normalized_resolutions = tuple(value.casefold() for value in excluded_resolutions)
    candidates = [
        choice
        for choice in active
        if not any(marker in choice.label.casefold() for marker in normalized_resolutions)
    ]
    if not candidates:
        raise ArchiveError(
            "only_resampled_torrents",
            "gallery only has excluded resampled torrents",
            ErrorClass.ITEM,
        )

    expected_size = _parse_size(estimated_size_raw, field="estimated")
    candidates = [choice for choice in candidates if choice.size_bytes * 5 >= expected_size * 4]
    if not candidates:
        raise ArchiveError(
            "torrent_size_too_small",
            "all usable torrents are smaller than 80% of the estimated gallery size",
            ErrorClass.ITEM,
        )

    survivors = [
        candidate
        for candidate in candidates
        if not any(
            other.size_bytes > candidate.size_bytes and other.posted_at > candidate.posted_at
            for other in candidates
        )
    ]
    if len(survivors) == 1:
        best = survivors[0]
    elif len({choice.size_bytes for choice in survivors}) == 1:
        best = max(survivors, key=lambda choice: (choice.seeds, choice.posted_at))
    else:
        raise ArchiveError(
            "ambiguous_torrent_versions",
            "remaining torrent versions cannot be ordered by both size and posted time",
            ErrorClass.ITEM,
        )
    if best.seeds == 0:
        raise ArchiveError(
            "latest_torrent_no_seeder",
            "selected latest torrent version has no seeder",
            ErrorClass.ITEM,
        )
    return best


class TorrentService:
    def __init__(
        self,
        *,
        http: Any,
        qbit: Any,
        torrent_root: str | Path,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self.http = http
        self.qbit = qbit
        # This is the path as seen by qBittorrent, not necessarily a local
        # filesystem path. The caller maps completed content separately.
        self.torrent_root = str(torrent_root)
        self.headers, self.cookies, self.proxies = headers or {}, cookies or {}, proxies

    def submit(
        self,
        manga_id: str,
        torrent_page_url: str,
        *,
        estimated_size_raw: str,
        skip_video: bool = False,
        excluded_resolutions: tuple[str, ...] = (),
        video_markers: tuple[str, ...] = (),
    ) -> tuple[str, TorrentChoice]:
        response = self.http.get(
            torrent_page_url,
            headers=self.headers,
            cookies=self.cookies,
            proxies=self.proxies,
            timeout=30,
        )
        response.raise_for_status()
        if "This gallery is currently unavailable" in response.text:
            raise ArchiveError("gallery_unavailable", "gallery is unavailable", ErrorClass.ITEM)
        if "There are no torrents for this gallery" in response.text:
            raise ArchiveError("no_torrent", "gallery has no torrent", ErrorClass.ITEM)
        choice = select_torrent(
            response.text,
            estimated_size_raw=estimated_size_raw,
            skip_video=skip_video,
            excluded_resolutions=excluded_resolutions or ("1280x", "800x", "1920x", "2560x"),
            video_markers=video_markers or ("mp4", "video"),
        )
        torrent_response = self.http.get(
            choice.url, headers=self.headers, cookies=self.cookies, proxies=self.proxies, timeout=30
        )
        torrent_response.raise_for_status()
        content = torrent_response.content
        if content.startswith(b"The torrent file could not be found") or not content.startswith(
            b"d"
        ):
            raise ArchiveError(
                "invalid_torrent", "torrent response is not a bencode dictionary", ErrorClass.ITEM
            )
        save_path = _join_external_path(self.torrent_root, safe_filename(manga_id.split("/", 1)[0]))
        torrent_hash = self.qbit.add(content, save_path=save_path)
        return torrent_hash, choice


def _join_external_path(root: str | Path, child: str) -> str:
    value = str(root)
    separator = "\\" if "\\" in value and "/" not in value else "/"
    value = value.rstrip("\\/")
    return f"{value}{separator}{child}" if value else child
