"""Frozen cleanup plans with fresh checks before each gallery is removed."""

import hashlib
import os
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select

from ....db.models import MangaRecord
from ....integrations.qbittorrent import is_managed_torrent
from .cleanup import (
    DatabaseState,
    FileCandidate,
    _classification,
    _remove_file_candidate,
    is_link,
    reconcile,
    torrent_identity,
)

SOURCES = ("torrent_download", "direct_download", "hah_download", "aria2_download")


def _raise_walk_error(error):
    raise error


def scope(app):
    return {
        "roots": {source: str(app.root(source).expanduser().resolve()) for source in SOURCES},
        "qbittorrent_url": app.qbittorrent_url,
    }


def file_identity(path, root):
    root = Path(root).resolve()
    path = Path(path)
    if is_link(path):
        raise ValueError("目标是符号链接或目录联接")
    resolved = path.resolve()
    if resolved.parent != root or resolved == root:
        raise ValueError("目标不在配置下载目录的直属层级")
    stat = path.stat()
    identity = {
        "path": str(resolved),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "kind": "directory" if path.is_dir() else "file",
    }
    if path.is_dir():
        digest = hashlib.sha256()
        for directory, directories, files in os.walk(
            path, followlinks=False, onerror=_raise_walk_error
        ):
            directories.sort()
            for name in sorted(directories + files):
                child = Path(directory) / name
                if is_link(child):
                    raise ValueError("目录内部包含链接，已保留整个目录")
                item = child.stat()
                digest.update(
                    repr(
                        (
                            str(child.relative_to(path)),
                            item.st_dev,
                            item.st_ino,
                            item.st_size,
                            item.st_mtime_ns,
                        )
                    ).encode()
                )
        identity["tree"] = digest.hexdigest()
    return identity


def torrent_snapshot(item):
    return {
        "name": str(item.name),
        "hash": str(item.hash),
        "save_path": str(getattr(item, "save_path", "")),
        "added_on": getattr(item, "added_on", None),
    }


def build_preview(*, database, app, qbit, only_id=None, checkpoint=lambda: None):
    checkpoint()
    report = reconcile(database=database, app=app, qbit=qbit, apply=False, only_id=only_id)
    report["scope"] = scope(app)
    for row in report["results"]:
        if row["action"] != "would_delete":
            continue
        checkpoint()
        try:
            if row["source"] == "qbittorrent":
                torrent_hash = row["target"].rsplit(" (", 1)[1][:-1]
                item = qbit.info(torrent_hash)
                if (
                    item is None
                    or not is_managed_torrent(item)
                    or torrent_identity(item) != (row["numeric_id"], torrent_hash)
                ):
                    raise ValueError("种子在扫描期间发生变化")
                row["identity"] = torrent_snapshot(item)
            else:
                row["identity"] = file_identity(row["target"], app.root(row["source"]))
        except (OSError, ValueError) as exc:
            row["action"], row["detail"] = "target_changed", str(exc)
    report["summary"] = dict(Counter(row["action"] for row in report["results"]))
    return report


def apply_preview(*, database, app, qbit, preview, checkpoint=lambda: None, on_result=None):
    if preview.get("mode") != "dry-run" or preview.get("scope") != scope(app):
        raise ValueError("下载目录或 qBittorrent 配置已改变，请重新扫描")
    groups = defaultdict(list)
    blocked_torrent_ids = set()
    for row in preview["results"]:
        if row["action"] == "would_delete":
            groups[row["numeric_id"]].append(dict(row))
        elif row["source"] == "qbittorrent":
            blocked_torrent_ids.add(row["numeric_id"])
    results = []

    def record(row, action, detail=None):
        row.update(action=action, detail=detail)
        results.append(row)
        if on_result:
            on_result(row)

    for numeric_id, rows in groups.items():
        checkpoint()
        # Hold the manga row lock until its external deletions finish. Ordinary
        # scheduling/status changes must wait rather than reuse these files.
        with database.session() as session:
            mangas = list(
                session.scalars(
                    select(MangaRecord)
                    .where(MangaRecord.manga_id.like(f"{numeric_id}/%"))
                    .with_for_update()
                )
            )
            manga = mangas[0] if len(mangas) == 1 else None
            state = (
                DatabaseState(
                    manga.manga_id,
                    manga.status,
                    manga.active_attempt_id is not None or manga.lease_token is not None,
                )
                if manga
                else None
            )
            action, detail = _classification(state)
            if len(mangas) > 1:
                action, detail = "target_changed", "数据库存在重复数字 ID"
            if action == "eligible" and any(
                r["database_manga_id"] != state.manga_id or r["database_status"] != state.status
                for r in rows
            ):
                action, detail = "target_changed", "档案状态与预览不一致，请重新扫描"
            if action != "eligible":
                for row in rows:
                    record(row, action, detail)
                continue

            blocked_torrent = numeric_id in blocked_torrent_ids
            for row in sorted(rows, key=lambda r: r["source"] != "qbittorrent"):
                source, identity = row["source"], row.get("identity")
                try:
                    if not identity:
                        raise ValueError("预览缺少目标身份信息，请重新扫描")
                    if source == "qbittorrent":
                        item = qbit.info(identity["hash"])
                        if item is None:
                            record(row, "already_missing")
                            continue
                        if not is_managed_torrent(item) or torrent_snapshot(item) != identity:
                            blocked_torrent = True
                            record(row, "target_changed", "种子归属或身份已改变")
                            continue
                        qbit.delete(identity["hash"], delete_files=False)
                        if qbit.info(identity["hash"]) is not None:
                            blocked_torrent = True
                            record(row, "delete_failed", "种子尚未移除，已保留下载目录")
                            continue
                    else:
                        if source not in SOURCES:
                            raise ValueError("未知下载来源")
                        if source == "torrent_download" and (
                            blocked_torrent
                            or any(
                                str(getattr(item, "name", "")) == numeric_id
                                for item in qbit.list_managed()
                            )
                        ):
                            record(
                                row, "torrent_delete_failed", "仍有种子任务或删除未确认，保留目录"
                            )
                            continue
                        path = Path(row["target"])
                        try:
                            current = file_identity(path, app.root(source))
                        except FileNotFoundError:
                            record(row, "already_missing")
                            continue
                        if current != identity:
                            record(row, "target_changed", "文件或目录与预览不一致")
                            continue
                        _remove_file_candidate(
                            FileCandidate(source, numeric_id, path, identity["kind"])
                        )
                    record(row, "deleted")
                except Exception as exc:  # noqa: BLE001 - report each failed external deletion
                    if source == "qbittorrent":
                        blocked_torrent = True
                    record(row, "delete_failed", str(exc))
    return {
        "mode": "apply",
        "only_id": preview.get("only_id"),
        "status_scope": preview["status_scope"],
        "summary": dict(Counter(row["action"] for row in results)),
        "results": results,
    }
