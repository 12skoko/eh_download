from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL, make_url

from eh_archive.db import Database
from eh_archive.db.models import EventLog, MangaInfoRecord, MangaRecord
from eh_archive.db.schema import upgrade
from eh_archive.services.paths import safe_filename
try:
    from scripts.migration_config import load_migration_config
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from migration_config import load_migration_config


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC)
    # Legacy ``mangainfo.fetchtime`` is an integer Unix timestamp while the
    # gallery fields are usually MySQL datetime values. Preserve both forms.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        try:
            return datetime.fromtimestamp(float(text), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC)


AUTO_MAPPING = {
    -1: ("deferred", None, "legacy_deferred"),
    1: ("discovered", None, "legacy_new"),
    2: ("download_pending", None, "legacy_queued"),
    3: ("skipped", None, "legacy_skipped"),
    4: ("downloading", "torrent", "legacy_torrent"),
    5: ("downloaded", "torrent", "legacy_torrent_done"),
    6: ("download_pending", "direct", "legacy_fallback"),
    7: ("downloading", "hah", "legacy_hah"),
    8: ("validating", "torrent", "legacy_torrent_info"),
    9: ("downloaded", "hah", "legacy_hah_done"),
    10: ("validating", "hah", "legacy_hah_archive"),
    11: ("validating", "direct", "legacy_direct"),
    12: ("manual_review", None, "legacy_conflict"),
    -2: ("manual_review", None, "legacy_unavailable"),
    -3: ("download_pending", None, "legacy_download_error"),
    -4: ("preparing", None, "legacy_compress_error"),
    -5: ("manual_review", None, "legacy_upload_error"),
    -6: ("manual_review", None, "legacy_video"),
}
STATE_MAPPING = {
    1: ("discovered", None, "legacy_new"),
    2: ("download_pending", None, "legacy_queued"),
    3: ("skipped", None, "legacy_skipped"),
    4: ("skipped", None, "legacy_skipped"),
    5: ("downloading", "torrent", "legacy_torrent"),
    14: ("downloading", "torrent", "legacy_priority_torrent"),
    6: ("download_pending", "direct", "legacy_fallback"),
    15: ("download_pending", "direct", "legacy_priority_fallback"),
    7: ("downloaded", "torrent", "legacy_torrent_done"),
    8: ("validating", "torrent", "legacy_torrent_info"),
    9: ("downloading", "hah", "legacy_hah"),
    10: ("downloaded", "hah", "legacy_hah_done"),
    11: ("validating", "direct", "legacy_direct"),
    12: ("validating", "hah", "legacy_archive"),
    13: ("download_pending", None, "legacy_priority"),
    -2: ("manual_review", None, "legacy_unavailable"),
    -3: ("download_pending", None, "legacy_download_error"),
    -4: ("preparing", None, "legacy_compress_error"),
    -5: ("manual_review", None, "legacy_upload_error"),
    -6: ("manual_review", None, "legacy_video"),
    -7: ("manual_review", None, "legacy_video"),
    -8: ("manual_review", None, "legacy_video"),
}


def legacy_details(row: dict[str, Any]) -> tuple[str, str | None, str, int, str]:
    """Return status, method, reason, priority and queue source for migration."""
    state, auto, remark = row.get("state"), row.get("autostate"), row.get("remark")
    if state == -1:
        return (
            ("deleted", None, "legacy_deleted", 0, "automatic")
            if remark == "deleted"
            else ("outdated", None, "legacy_outdated", 0, "automatic")
        )
    if state == 0:
        reason = (
            "legacy_complete" if valid_sha1(row.get("arcid")) else "legacy_completed_without_lrr_id"
        )
        return "completed", None, reason, 0, "automatic"
    if auto is not None:
        status, method, reason = AUTO_MAPPING.get(
            auto, ("manual_review", None, "unknown_legacy_autostate")
        )
        return status, method, reason, 0, "automatic"
    status, method, reason = STATE_MAPPING.get(
        state, ("manual_review", None, "unknown_legacy_state")
    )
    return status, method, reason, 100 if state in {13, 14, 15} else 0, "manual"


def map_legacy_status(row: dict[str, Any]) -> tuple[str, str, int]:
    """Compatibility helper exposing the migration status decision only."""
    status, _method, reason, priority, _source = legacy_details(row)
    return status, reason, priority


def valid_sha1(value: Any) -> str | None:
    text = str(value or "")
    return text if re.fullmatch(r"[0-9a-fA-F]{40}", text) else None


def legacy_artifact_location(method: str | None, filename: str) -> str:
    """Map a legacy filename to one of the current controlled roots."""

    if filename.lower().endswith(".zip"):
        return "prepared"
    return {
        "direct": "direct_download",
        "aria2": "aria2_download",
        "hah": "hah_download",
        "torrent": "torrent_download",
    }.get(method or "torrent", "torrent_download")


def migrate(
    mysql_rows: list[dict[str, Any]],
    info_rows: list[dict[str, Any]],
    database: Database,
    *,
    apply: bool,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "manga_rows": len(mysql_rows),
        "info_rows": len(info_rows),
        "created": 0,
        "manual_review": 0,
        "errors": [],
        "mapping_counts": {},
        "time_parse_failures": [],
    }
    if not apply:
        for row in mysql_rows:
            status, _method, reason, _priority, _source = legacy_details(row)
            key = f"state={row.get('state')!r};autostate={row.get('autostate')!r};{status};{reason}"
            report.setdefault("mapping_counts", {}).setdefault(key, 0)
            report["mapping_counts"][key] += 1
            if status == "manual_review":
                report["manual_review"] += 1
        return report
    upgrade(database)
    with database.session() as session:
        known: set[str] = set()
        for row in mysql_rows:
            manga_id = str(row.get("manga_id", ""))
            if not manga_id:
                report["errors"].append("manga row without manga_id")
                continue
            status, method, reason, priority, source = legacy_details(row)
            key = f"state={row.get('state')!r};autostate={row.get('autostate')!r};{status};{reason}"
            report["mapping_counts"][key] = report["mapping_counts"].get(key, 0) + 1
            record = session.get(MangaRecord, manga_id)
            if record is None:
                record = MangaRecord(manga_id=manga_id)
                session.add(record)
            record.name = str(row.get("name") or "")
            record.real_name = str(row.get("realname") or "")
            record.link = str(row.get("link") or "")
            record.torrent_link = str(row.get("torrentlink") or "")
            record.posted_at = parse_datetime(row.get("postedtime"))
            if row.get("postedtime") and record.posted_at is None:
                report["time_parse_failures"].append(
                    {
                        "manga_id": manga_id,
                        "field": "postedtime",
                        "value": str(row.get("postedtime")),
                    }
                )
            record.category = str(row.get("category") or "")
            record.tags_raw = str(row.get("tag") or "")
            record.pages, record.rating, record.uploader = (
                row.get("pages"),
                row.get("rating"),
                str(row.get("uploader") or ""),
            )
            record.remark, record.queue_source = row.get("remark"), source
            record.status, record.priority = status, priority
            record.download_method = method
            record.external_download_id = row.get("torrenthash")
            legacy_filename = str(row.get("filename") or "")
            try:
                record.artifact_filename = (
                    safe_filename(legacy_filename) if legacy_filename else None
                )
            except ValueError:
                record.artifact_filename = None
                status, reason = "manual_review", "unsafe_legacy_filename"
            record.lrr_archive_id = valid_sha1(row.get("arcid"))
            if row.get("arcid") and not record.lrr_archive_id:
                status, reason = "manual_review", "invalid_legacy_archive_id"
            record.status = status
            if record.artifact_filename:
                is_zip = record.artifact_filename.lower().endswith(".zip")
                location = legacy_artifact_location(method, record.artifact_filename)
                record.artifact_location = location
                record.artifact_kind = "zip" if is_zip else "directory"
                record.artifact_generation = 1
            session.flush()
            session.add(
                EventLog(
                    manga_id=manga_id,
                    component="migration",
                    event_type="legacy_import",
                    actor="migration",
                    to_status=record.status,
                    detail={
                        "reason": reason,
                        "legacy_state": row.get("state"),
                        "legacy_autostate": row.get("autostate"),
                        "legacy_remark": row.get("remark"),
                        "queue_source": source,
                        "priority": priority,
                    },
                )
            )
            known.add(manga_id)
            report["created"] += 1
        for row in info_rows:
            manga_id = str(row.get("manga_id", ""))
            if not manga_id:
                continue
            if manga_id not in known and session.get(MangaRecord, manga_id) is None:
                session.add(
                    MangaRecord(
                        manga_id=manga_id,
                        name=str(row.get("name") or manga_id),
                        link=str(row.get("link") or ""),
                        status="manual_review",
                        queue_source="manual",
                    )
                )
                session.flush()
                session.add(
                    EventLog(
                        manga_id=manga_id,
                        component="migration",
                        event_type="legacy_orphan_info",
                        actor="migration",
                        to_status="manual_review",
                        detail={"reason": "orphan_mangainfo"},
                    )
                )
                report["manual_review"] += 1
            info = session.get(MangaInfoRecord, manga_id)
            if info is None:
                info = MangaInfoRecord(manga_id=manga_id)
                session.add(info)
            text_fields = {
                "name",
                "roman_name",
                "real_name",
                "link",
                "category",
                "uploader",
                "language",
                "estimated_size_raw",
                "tags_raw",
                "tags_translated_raw",
            }
            for target, source in {
                "name": "name",
                "roman_name": "romaname",
                "real_name": "realname",
                "link": "link",
                "category": "category",
                "uploader": "uploader",
                "language": "language",
                "estimated_size_raw": "estimatedsize",
                "pages": "pages",
                "favorited": "favorited",
                "rating_count": "ratingcount",
                "rating": "rating",
                "tags_raw": "tag",
                "tags_translated_raw": "tagtran",
            }.items():
                value = row.get(source)
                setattr(info, target, str(value or "") if target in text_fields else value)
            info.posted_at, info.fetched_at = (
                parse_datetime(row.get("postedtime")),
                parse_datetime(row.get("fetchtime")),
            )
            if row.get("postedtime") and info.posted_at is None:
                report["time_parse_failures"].append(
                    {
                        "manga_id": manga_id,
                        "field": "postedtime",
                        "value": str(row.get("postedtime")),
                    }
                )
            if row.get("fetchtime") and info.fetched_at is None:
                report["time_parse_failures"].append(
                    {"manga_id": manga_id, "field": "fetchtime", "value": str(row.get("fetchtime"))}
                )
    return report


def _mysql_rows(url: str | URL) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        import pymysql
    except ImportError as exc:
        raise SystemExit("install the migration extra for PyMySQL") from exc
    parsed = make_url(url) if isinstance(url, str) else url
    connection = pymysql.connect(
        host=parsed.host or "localhost",
        port=parsed.port or 3306,
        user=parsed.username or "",
        password=parsed.password or "",
        database=parsed.database or "",
        cursorclass=pymysql.cursors.DictCursor,
        read_timeout=60,
        write_timeout=60,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM manga")
            manga = list(cursor.fetchall())
            cursor.execute("SELECT * FROM mangainfo")
            info = list(cursor.fetchall())
        return manga, info
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        help="TOML file containing structured [mysql] and [postgres] connection settings",
    )
    parser.add_argument("--mysql", help="legacy MySQL URL (alternative to --config)")
    parser.add_argument("--postgres", help="target PostgreSQL URL (alternative to --config)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write the migration to PostgreSQL")
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="read MySQL and emit a report without writing PostgreSQL",
    )
    parser.add_argument("--report", default="migration-report.json")
    args = parser.parse_args(argv)
    if args.config:
        if args.mysql or args.postgres:
            parser.error("--config cannot be combined with --mysql or --postgres")
        try:
            mysql_url, postgres_url = load_migration_config(Path(args.config))
        except ValueError as exc:
            parser.error(str(exc))
    elif not args.mysql or not args.postgres:
        parser.error("provide --config, or provide both --mysql and --postgres")
    else:
        mysql_url, postgres_url = args.mysql, args.postgres
    manga, info = _mysql_rows(mysql_url)
    result = migrate(manga, info, Database(postgres_url), apply=bool(args.apply))
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
