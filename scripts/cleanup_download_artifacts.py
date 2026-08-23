"""Remove terminal download artifacts left behind by failed cleanup runs.

This is an operator tool, not part of the Supervisor workflow. It never changes
PostgreSQL state and never calls LANraragi. Preview is the default; filesystem
and qBittorrent mutations require ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from eh_archive.config import AppConfig, load_config
from eh_archive.db import Database, MangaRecord
from eh_archive.integrations.qbittorrent import QBittorrentClient

TERMINAL_STATUSES = frozenset({"completed", "deleted"})
DIRECT_NAME = re.compile(r"^\[(\d+)](?=.+\.zip$)", re.IGNORECASE)
HAH_NAME = re.compile(r"^\[(\d+)]")
ARIA2_NAME = re.compile(r"^(\d+)_[A-Za-z0-9._-]+\.g\d+\.zip$", re.IGNORECASE)
ARIA2_LEGACY_NAME = re.compile(r"^\[(\d+)].+\.zip$", re.IGNORECASE)


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
    match = DIRECT_NAME.match(entry.name) if entry.is_file() else None
    return match.group(1) if match else None


def _hah_id(entry: Path) -> str | None:
    match = HAH_NAME.match(entry.name) if entry.is_dir() else None
    if match is None or not (entry / "galleryinfo.txt").is_file():
        return None
    return match.group(1)


def _aria2_id(entry: Path) -> str | None:
    if not entry.is_file():
        return None
    match = ARIA2_NAME.match(entry.name) or ARIA2_LEGACY_NAME.match(entry.name)
    return match.group(1) if match else None


def scan_download_roots(
    app: AppConfig, *, only_id: str | None = None
) -> tuple[list[FileCandidate], list[Result]]:
    candidates: list[FileCandidate] = []
    skipped: list[Result] = []
    scanners = {
        "torrent_download": lambda entry: entry.name if entry.is_dir() and entry.name.isdigit() else None,
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
            if entry.is_symlink():
                skipped.append(Result(source, None, str(entry), None, None, "symlink_skipped"))
                continue
            numeric_id = identify(entry)
            if numeric_id is None:
                skipped.append(Result(source, None, str(entry), None, None, "name_not_recognized"))
                continue
            if only_id is not None and numeric_id != only_id:
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
        return "status_skipped", f"status is {state.status}"
    return "eligible", None


def _remove_file_candidate(candidate: FileCandidate) -> None:
    path = candidate.path
    if path.is_symlink():
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
    only_id: str | None = None,
) -> dict[str, Any]:
    file_candidates, results = scan_download_roots(app, only_id=only_id)
    torrent_items = qbit.list_managed()
    torrent_candidates: list[tuple[str, str, str]] = []
    for item in torrent_items:
        identity = torrent_identity(item)
        name = str(getattr(item, "name", "") or "<unnamed>")
        if identity is None:
            results.append(
                Result("qbittorrent", None, name, None, None, "name_not_recognized")
            )
            continue
        numeric_id, torrent_hash = identity
        if only_id is not None and numeric_id != only_id:
            continue
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
        "only_id": only_id,
        "summary": dict(sorted(counts.items())),
        "results": [asdict(item) for item in results],
    }


def _report_path(log_dir: Path, timezone: str) -> Path:
    timestamp = datetime.now(ZoneInfo(timezone)).strftime("%Y%m%d-%H%M%S-%f")
    directory = log_dir / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"cleanup_download_artifacts-{timestamp}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Remove qBittorrent tasks and download artifacts whose database status is "
            "completed or deleted. Preview is the default."
        )
    )
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--apply", action="store_true", help="perform deletions")
    parser.add_argument("--id", dest="only_id", help="limit the scan to one numeric manga ID")
    parser.add_argument("--report", type=Path, help="write JSON to this path")
    args = parser.parse_args(argv)
    if args.only_id is not None and not args.only_id.isdigit():
        parser.error("--id must contain only digits")

    app, _, _, secrets = load_config(args.config_dir)
    options = dict(secrets.qbittorrent)
    options.setdefault("host", app.qbittorrent_url)
    database = Database(app.database_url)
    try:
        report = reconcile(
            database=database,
            app=app,
            qbit=QBittorrentClient(**options),
            apply=args.apply,
            only_id=args.only_id,
        )
    finally:
        database.dispose()

    report["generated_at"] = datetime.now(ZoneInfo(app.timezone)).isoformat()
    path = args.report or _report_path(app.log_dir, app.timezone)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Mode: {report['mode']}")
    print(f"JSON written: {path.resolve()}")
    failed = report["summary"].get("delete_failed", 0) + report["summary"].get(
        "torrent_delete_failed", 0
    )
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
