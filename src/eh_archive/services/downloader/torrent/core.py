from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ....domain.errors import ArchiveError, ErrorClass
from ...paths import safe_filename


@dataclass(frozen=True)
class TorrentChoice:
    url: str
    size: str
    seeds: int
    label: str


def select_torrent(
    html: str,
    *,
    skip_video: bool = False,
    excluded_resolutions: tuple[str, ...] = ("1280x", "800x", "1920x", "2560x"),
    video_markers: tuple[str, ...] = ("mp4", "video"),
) -> TorrentChoice | None:
    pattern = re.compile(
        r"Posted:</span>\s*<span>(.*?)</span></td>.*?Size:</span>\s*(.*?)</td>.*?"
        r"Seeds:</span>\s*(\d+)</td>.*?Peers:</span>\s*\d+</td>.*?Downloads:</span>\s*\d+</td>"
        r".*?<a href=\"(.*?)\" onclick=\"document\.location='(.*?)'; return false\">(.*?)</a></td>",
        re.DOTALL,
    )
    rows: list[TorrentChoice] = []
    for match in pattern.finditer(html):
        _, size, seeds, _, url, label = match.groups()
        label = re.sub(r"<[^>]+>", "", label)
        if any(item in label for item in excluded_resolutions):
            continue
        if not skip_video and any(item in label.lower() for item in video_markers):
            raise ArchiveError(
                "video_torrent", "torrent list contains a video archive", ErrorClass.ITEM
            )
        count = int(seeds)
        if count > 0:
            rows.append(TorrentChoice(url, size.strip(), count, label.strip()))
    if not rows:
        return None
    best = rows[0]
    for candidate in rows[1:]:
        if candidate.size == best.size and candidate.seeds > best.seeds:
            best = candidate
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
        category: str = "eharchive",
    ) -> None:
        self.http = http
        self.qbit = qbit
        # This is the path as seen by qBittorrent, not necessarily a local
        # filesystem path. The caller maps completed content separately.
        self.torrent_root = str(torrent_root)
        self.headers, self.cookies, self.proxies = headers or {}, cookies or {}, proxies
        self.category = category

    def submit(
        self,
        manga_id: str,
        torrent_page_url: str,
        *,
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
            skip_video=skip_video,
            excluded_resolutions=excluded_resolutions or ("1280x", "800x", "1920x", "2560x"),
            video_markers=video_markers or ("mp4", "video"),
        )
        if choice is None:
            raise ArchiveError("no_seeded_torrent", "no seeded usable torrent", ErrorClass.ITEM)
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
        torrent_hash = self.qbit.add(content, save_path=save_path, category=self.category)
        return torrent_hash, choice


def _join_external_path(root: str | Path, child: str) -> str:
    value = str(root)
    separator = "\\" if "\\" in value and "/" not in value else "/"
    value = value.rstrip("\\/")
    return f"{value}{separator}{child}" if value else child
