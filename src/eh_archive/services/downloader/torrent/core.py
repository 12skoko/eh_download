from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    personalized_url: str | None = None


@dataclass(frozen=True)
class TorrentOption:
    choice_id: str
    site_id: str
    url: str
    size: str
    size_bytes: int
    seeds: int
    posted_at: datetime
    label: str
    page_order: int
    outdated: bool
    red_date: bool
    resampled: bool
    video: bool
    personalized_url: str | None = None

    @property
    def suggested_role(self) -> str:
        return "video" if self.video else "image"

    @property
    def warnings(self) -> tuple[str, ...]:
        values = []
        if self.seeds == 0:
            values.append("no_seeders")
        if self.outdated:
            values.append("outdated")
        if self.red_date:
            values.append("red_date")
        if self.resampled:
            values.append("resampled")
        return tuple(values)

    def public_snapshot(self) -> dict[str, Any]:
        return {
            "choice_id": self.choice_id,
            "site_id": self.site_id,
            "label": self.label,
            "size": self.size,
            "size_bytes": self.size_bytes,
            "seeds": self.seeds,
            "posted_at": self.posted_at.isoformat(),
            "page_order": self.page_order,
            "outdated": self.outdated,
            "red_date": self.red_date,
            "resampled": self.resampled,
            "video": self.video,
            "suggested_role": self.suggested_role,
            "warnings": list(self.warnings),
        }


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
            ErrorClass.SYSTEM,
        )
    label = marker.get_text(" ", strip=True)
    text = cell.get_text(" ", strip=True)
    value = text[len(label) :].strip() if text.startswith(label) else ""
    if not value:
        raise ArchiveError(
            "torrent_list_parse_error",
            f"torrent row has an empty {name}",
            ErrorClass.SYSTEM,
        )
    return value, cell


def _download_url(anchor: Any) -> str:
    return str(anchor.get("href", "")).strip()


def _personalized_url(anchor: Any) -> str | None:
    """Read the known assignment syntax; never evaluate page JavaScript."""
    match = re.fullmatch(
        r"\s*document\.location\s*=\s*(['\"])(https://[^'\"\s]+)\1\s*;\s*return\s+false\s*;?\s*",
        str(anchor.get("onclick", "")),
    )
    if not match:
        return None
    public, private = urlsplit(_download_url(anchor)), urlsplit(match[2])
    if (public.scheme != "https" or public.hostname not in {"exhentai.org", "e-hentai.org"}
            or private.netloc != public.netloc or private.query or private.fragment
            or private.username or private.password):
        return None
    plain = re.fullmatch(r"/torrent/(\d+)/([a-fA-F0-9]{40}\.torrent)", public.path)
    tracked = re.fullmatch(r"/torrent/(\d+)/[A-Za-z0-9-]+/([a-fA-F0-9]{40}\.torrent)", private.path)
    return match[2] if plain and tracked and plain.groups() == tracked.groups() else None


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
        (item for item in form.find_all("a", href=True) if ".torrent" in str(item.get("href", ""))),
        None,
    )
    if anchor is None:
        raise ArchiveError(
            "torrent_list_parse_error",
            "torrent row has no download link",
            ErrorClass.SYSTEM,
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
            ErrorClass.SYSTEM,
        ) from exc
    if not url or not label or seeds < 0:
        raise ArchiveError(
            "torrent_list_parse_error",
            "torrent row has an invalid URL, title, or seed count",
            ErrorClass.SYSTEM,
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
            personalized_url=_personalized_url(anchor),
        ),
        outdated,
    )


def parse_torrent_options(
    html: str,
    *,
    excluded_resolutions: tuple[str, ...] = ("1280x", "800x", "1920x", "2560x"),
    video_markers: tuple[str, ...] = ("mp4", "video"),
    include_outdated: bool = True,
    bind_download_url: bool = False,
) -> list[TorrentOption]:
    """Parse every torrent row while keeping private download URLs server-side."""

    soup = BeautifulSoup(html, "lxml")
    options: list[TorrentOption] = []
    outdated_section = False
    skipped_outdated = False
    page_order = 0
    normalized_resolutions = tuple(value.casefold() for value in excluded_resolutions)
    normalized_video = tuple(value.casefold() for value in video_markers)
    for node in soup.find_all(["p", "form"]):
        if node.name == "p":
            if node.get_text(" ", strip=True).casefold() == "outdated torrents:":
                outdated_section = True
            continue
        input_node = node.find("input", attrs={"name": "gtid"})
        if input_node is None:
            continue
        if outdated_section and not include_outdated:
            skipped_outdated = True
            continue
        posted_raw, posted_cell = _field_text(node, "Posted")
        red_date = any(
            "color:red" in str(span.get("style", "")).replace(" ", "").casefold()
            for span in posted_cell.find_all("span")
        )
        if red_date and not include_outdated:
            skipped_outdated = True
            continue
        size_raw, _ = _field_text(node, "Size")
        seeds_raw, _ = _field_text(node, "Seeds")
        anchor = next(
            (
                item
                for item in node.find_all("a", href=True)
                if ".torrent" in str(item.get("href", ""))
            ),
            None,
        )
        if anchor is None:
            raise ArchiveError(
                "torrent_list_parse_error", "torrent row has no download link", ErrorClass.SYSTEM
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
                ErrorClass.SYSTEM,
            ) from exc
        if not url or not label or seeds < 0:
            raise ArchiveError(
                "torrent_list_parse_error",
                "torrent row has an invalid URL, title, or seed count",
                ErrorClass.SYSTEM,
            )
        site_id = str(input_node.get("value", "")).strip()
        stable = f"{site_id}\x1f{label}\x1f{posted_raw}\x1f{size_raw}"
        if bind_download_url:
            stable += f"\x1f{url}"
        choice_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]
        if not site_id:
            site_id = f"derived-{choice_id}"
        normalized_label = label.casefold()
        options.append(
            TorrentOption(
                choice_id=choice_id,
                site_id=site_id,
                url=url,
                size=size_raw,
                size_bytes=_parse_size(size_raw, field="torrent"),
                seeds=seeds,
                posted_at=posted_at,
                label=label,
                page_order=page_order,
                outdated=outdated_section or red_date,
                red_date=red_date,
                resampled=any(value in normalized_label for value in normalized_resolutions),
                video=any(value in normalized_label for value in normalized_video),
                personalized_url=_personalized_url(anchor),
            )
        )
        page_order += 1
    if not options:
        if skipped_outdated:
            raise ArchiveError("only_outdated_torrents", "gallery only has outdated torrents", ErrorClass.ITEM)
        if "There are no torrents for this gallery" in html:
            raise ArchiveError("no_torrent", "gallery has no torrent", ErrorClass.ITEM)
        raise ArchiveError(
            "torrent_list_parse_error",
            "torrent page contains no recognizable torrent rows",
            ErrorClass.SYSTEM,
        )
    return options


def select_torrent(
    html: str,
    *,
    estimated_size_raw: str,
    skip_video: bool = False,
    skip_small: bool = False,
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
                ErrorClass.SYSTEM,
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
            ErrorClass.SYSTEM,
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
    if not skip_small:
        candidates = [choice for choice in candidates if choice.size_bytes * 5 >= expected_size * 3]
    if not candidates:
        raise ArchiveError(
            "torrent_size_too_small",
            "all usable torrents are smaller than 60% of the estimated gallery size",
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
        upload_limit_bytes_per_second: int | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self.http = http
        self.qbit = qbit
        # This is the path as seen by qBittorrent, not necessarily a local
        # filesystem path. The caller maps completed content separately.
        self.torrent_root = str(torrent_root)
        self.upload_limit_bytes_per_second = upload_limit_bytes_per_second
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
        review: dict | None = None,
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
        from .review import choose_with_review, download_torrent

        choice, decision = choose_with_review(
            response.text,
            estimated_size_raw=estimated_size_raw,
            skip_video=skip_video,
            excluded_resolutions=excluded_resolutions or ("1280x", "800x", "1920x", "2560x"),
            video_markers=video_markers or ("mp4", "video"),
            review=review,
        )
        content = download_torrent(
            self.http, choice, decision=decision,
            request_options={"headers": self.headers, "cookies": self.cookies,
                             "proxies": self.proxies, "timeout": 30},
        )
        idnum = safe_filename(manga_id.split("/", 1)[0])
        save_path = _join_external_path(self.torrent_root, idnum)
        add_options: dict[str, Any] = {
            "save_path": save_path,
            "display_name": idnum,
        }
        if self.upload_limit_bytes_per_second is not None:
            add_options["upload_limit_bytes_per_second"] = self.upload_limit_bytes_per_second
        torrent_hash = self.qbit.add(content, **add_options)
        return torrent_hash, choice


def _join_external_path(root: str | Path, child: str) -> str:
    value = str(root)
    separator = "\\" if "\\" in value and "/" not in value else "/"
    value = value.rstrip("\\/")
    return f"{value}{separator}{child}" if value else child
