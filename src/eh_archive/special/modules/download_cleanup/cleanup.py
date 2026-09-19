"""Remove terminal download artifacts left behind by failed cleanup runs.

Shared by the command-line tool and the manual Web cleanup workflow.
Manga state and LANraragi archives are never modified.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from eh_archive.config import AppConfig
from eh_archive.db import Database, MangaRecord
from eh_archive.integrations.qbittorrent import QBittorrentClient

TERMINAL_STATUSES = frozenset({"completed", "deleted"})
DIRECT_NAME = re.compile(r"^\[(\d+)](?=.+\.zip$)", re.IGNORECASE)
HAH_NAME = re.compile(r"^\[(\d+)]")
ARIA2_NAME = re.compile(r"^(\d+)_[A-Za-z0-9._-]+\.g\d+\.zip$", re.IGNORECASE)
ARIA2_LEGACY_NAME = re.compile(r"^\[(\d+)].+\.zip$", re.IGNORECASE)
TEMPORARY_NAME = re.compile(
    r"^(\d+)_[A-Za-z0-9._-]+\.g\d+\.a(?:\d+|pending)\.tmp(?:\.part)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DatabaseState:
    manga_id: str
    status: str
    has_active_task: bool


@dataclass(frozen=True)
class FileCandidate:
    source: str
    numeric_id: str
    path: Path
    entry_kind: str


@dataclass
class Result:
    source: str
    numeric_id: str | None
    target: str
    database_manga_id: str | None
    database_status: str | None
    action: str
    detail: str | None = None


def numeric_manga_id(manga_id: str) -> str | None:
    value, separator, _ = manga_id.partition("/")
    return value if separator and value.isdigit() else None


def torrent_identity(item: Any) -> tuple[str, str] | None:
    name = str(getattr(item, "name", "") or "")
    torrent_hash = str(getattr(item, "hash", "") or "")
    if not name.isdigit() or not torrent_hash:
        return None
    return name, torrent_hash


def _direct_id(entry: Path) -> str | None:
    if not entry.is_file():
        return None
    match = DIRECT_NAME.match(entry.name) or TEMPORARY_NAME.fullmatch(entry.name)
    return match.group(1) if match else None


def _hah_id(entry: Path) -> str | None:
    match = HAH_NAME.match(entry.name) if entry.is_dir() else None
    if match is None or not (entry / "galleryinfo.txt").is_file():
        return None
    return match.group(1)


def _aria2_id(entry: Path) -> str | None:
    if not entry.is_file():
        return None
    # A control file belongs to the same download as the name before .aria2.
    name = entry.name[:-6] if entry.name.lower().endswith(".aria2") else entry.name
    match = (
        ARIA2_NAME.fullmatch(name)
        or ARIA2_LEGACY_NAME.fullmatch(name)
        or TEMPORARY_NAME.fullmatch(name)
    )
    return match.group(1) if match else None


def is_link(path: Path) -> bool:
    # Python 3.11 has no Path.is_junction(); Windows reparse points cover both.
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def scan_download_roots(app: AppConfig) -> tuple[list[FileCandidate], list[Result]]:
    candidates: list[FileCandidate] = []
    skipped: list[Result] = []
    scanners = {
        "torrent_download": lambda entry: (
            entry.name if entry.is_dir() and entry.name.isdigit() else None
        ),
        "direct_download": _direct_id,
        "hah_download": _hah_id,
        "aria2_download": _aria2_id,
    }
    for source, identify in scanners.items():
        root = app.root(source).expanduser()
        if not root.exists():
            skipped.append(Result(source, None, str(root), None, None, "root_missing"))
            continue
        if not root.is_dir():
            skipped.append(Result(source, None, str(root), None, None, "root_not_directory"))
            continue
        for entry in root.iterdir():
            if is_link(entry):
                skipped.append(Result(source, None, str(entry), None, None, "symlink_skipped"))
                continue
            numeric_id = identify(entry)
            if numeric_id is None:
                skipped.append(Result(source, None, str(entry), None, None, "name_not_recognized"))
                continue
            candidates.append(
                FileCandidate(
                    source=source,
                    numeric_id=numeric_id,
                    path=entry,
                    entry_kind="directory" if entry.is_dir() else "file",
                )
            )
    return candidates, skipped


def load_database_states(database: Database, candidate_ids: set[str]) -> dict[str, DatabaseState]:
    states: dict[str, DatabaseState] = {}
    if not candidate_ids:
        return states
    with database.session() as session:
        rows = session.execute(
            select(
                MangaRecord.manga_id,
                MangaRecord.status,
                MangaRecord.active_attempt_id,
                MangaRecord.lease_token,
            ).execution_options(yield_per=10_000)
        )
        for manga_id, status, active_attempt_id, lease_token in rows:
            numeric_id = numeric_manga_id(manga_id)
            if numeric_id not in candidate_ids:
                continue
            state = DatabaseState(
                manga_id=manga_id,
                status=status,
                has_active_task=active_attempt_id is not None or lease_token is not None,
            )
            previous = states.get(numeric_id)
            if previous is not None and previous.manga_id != manga_id:
                raise RuntimeError(f"duplicate numeric manga ID in database: {numeric_id}")
            states[numeric_id] = state
    return states


def _classification(state: DatabaseState | None) -> tuple[str, str | None]:
    if state is None:
        return "database_not_found", None
    if state.has_active_task:
        return "active_task_skipped", "terminal row still has an active attempt or lease"
    if state.status not in TERMINAL_STATUSES:
        return "status_skipped", None
    return "eligible", None


def _remove_file_candidate(candidate: FileCandidate) -> None:
    path = candidate.path
    if path.exists() and is_link(path):
        raise RuntimeError("target became a symlink after scanning")
    if not path.exists():
        return
    if candidate.entry_kind == "directory":
        if not path.is_dir():
            raise RuntimeError("target type changed after scanning")
        shutil.rmtree(path)
    else:
        if not path.is_file():
            raise RuntimeError("target type changed after scanning")
        path.unlink()


def reconcile(
    *,
    database: Database,
    app: AppConfig,
    qbit: QBittorrentClient,
    apply: bool,
) -> dict[str, Any]:
    file_candidates, results = scan_download_roots(app)
    torrent_items = qbit.list_managed()
    torrent_candidates: list[tuple[str, str, str]] = []
    for item in torrent_items:
        identity = torrent_identity(item)
        name = str(getattr(item, "name", "") or "<unnamed>")
        if identity is None:
            results.append(Result("qbittorrent", None, name, None, None, "name_not_recognized"))
            continue
        numeric_id, torrent_hash = identity
        torrent_candidates.append((numeric_id, torrent_hash, f"{name} ({torrent_hash})"))

    candidate_ids = {item.numeric_id for item in file_candidates}
    candidate_ids.update(item[0] for item in torrent_candidates)
    states = load_database_states(database, candidate_ids)
    torrent_failures: set[str] = set()

    for numeric_id, torrent_hash, target in torrent_candidates:
        state = states.get(numeric_id)
        action, detail = _classification(state)
        if action == "eligible":
            action = "would_delete" if not apply else "deleted"
            if apply:
                try:
                    qbit.delete(torrent_hash, delete_files=False)
                except Exception as exc:  # noqa: BLE001 - one failed task must not hide the report
                    action = "delete_failed"
                    detail = str(exc)
                    torrent_failures.add(numeric_id)
        results.append(
            Result(
                "qbittorrent",
                numeric_id,
                target,
                state.manga_id if state else None,
                state.status if state else None,
                action,
                detail,
            )
        )

    for candidate in file_candidates:
        state = states.get(candidate.numeric_id)
        action, detail = _classification(state)
        if action == "eligible":
            if candidate.source == "torrent_download" and candidate.numeric_id in torrent_failures:
                action = "torrent_delete_failed"
                detail = "qBittorrent task deletion failed; directory was preserved"
            else:
                action = "would_delete" if not apply else "deleted"
                if apply:
                    try:
                        _remove_file_candidate(candidate)
                    except Exception as exc:  # noqa: BLE001 - continue and report every target
                        action = "delete_failed"
                        detail = str(exc)
        results.append(
            Result(
                candidate.source,
                candidate.numeric_id,
                str(candidate.path),
                state.manga_id if state else None,
                state.status if state else None,
                action,
                detail,
            )
        )

    counts = Counter(item.action for item in results)
    return {
        "mode": "apply" if apply else "dry-run",
        "status_scope": sorted(TERMINAL_STATUSES),
        "summary": dict(sorted(counts.items())),
        "results": [asdict(item) for item in results],
    }
