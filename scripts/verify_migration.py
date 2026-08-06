from __future__ import annotations

import argparse
import json
from collections import Counter

from sqlalchemy import func, select

from eh_archive.db import Database
from eh_archive.db.models import EventLog, MangaInfoRecord, MangaRecord

if __package__:
    from .migration_config import load_migration_config
else:
    from migration_config import load_migration_config


def _duplicates(session, column):
    return [
        {"value": value, "count": count}
        for value, count in session.execute(
            select(column, func.count())
            .where(column.is_not(None))
            .group_by(column)
            .having(func.count() > 1)
        )
    ]


def verify(database: Database) -> dict:
    with database.session() as session:
        manga = list(session.query(MangaRecord))
        info = list(session.query(MangaInfoRecord))
        orphan_info = list(
            session.scalars(
                select(MangaInfoRecord.manga_id)
                .outerjoin(MangaRecord, MangaInfoRecord.manga_id == MangaRecord.manga_id)
                .where(MangaRecord.manga_id.is_(None))
            )
        )
        migration_events = list(
            session.scalars(
                select(EventLog).where(
                    EventLog.component == "migration",
                    EventLog.event_type.in_(("legacy_import", "legacy_orphan_info")),
                )
            )
        )
        return {
            "manga": len(manga),
            "mangainfo": len(info),
            "statuses": dict(Counter(row.status for row in manga)),
            "missing_details": sum(
                1 for row in manga if session.get(MangaInfoRecord, row.manga_id) is None
            ),
            "orphan_mangainfo": orphan_info,
            "duplicate_lrr_archive_ids": _duplicates(session, MangaRecord.lrr_archive_id),
            "duplicate_external_download_ids": _duplicates(
                session, MangaRecord.external_download_id
            ),
            "invalid_lrr_archive_ids": [
                row.manga_id
                for row in manga
                if row.lrr_archive_id is not None and len(row.lrr_archive_id) != 40
            ],
            "null_business_fields": {
                field: sum(getattr(row, field) is None for row in manga)
                for field in ("name", "link", "category", "tags_raw", "uploader")
            },
            "artifact_missing_fingerprint": [
                row.manga_id
                for row in manga
                if row.artifact_filename
                and not all(
                    value is not None
                    for value in (
                        row.artifact_location,
                        row.artifact_kind,
                        row.artifact_generation,
                        row.artifact_sha1,
                    )
                )
            ],
            "migration_audit_events": len(migration_events),
        }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="migration TOML file for both database connections")
    parser.add_argument("--postgres", help="target PostgreSQL URL (alternative to --config)")
    parser.add_argument(
        "--mysql", help="optional read-only MySQL URL for source row-count comparison"
    )
    args = parser.parse_args(argv)
    if args.config:
        if args.mysql or args.postgres:
            parser.error("--config cannot be combined with --mysql or --postgres")
        try:
            mysql_url, postgres_url = load_migration_config(args.config)
        except ValueError as exc:
            parser.error(str(exc))
    elif not args.postgres:
        parser.error("provide --config or --postgres")
    else:
        mysql_url, postgres_url = args.mysql, args.postgres
    result = verify(Database(postgres_url))
    if mysql_url:
        if __package__:
            from .migrate_mysql_to_postgresql import _mysql_rows
        else:
            from migrate_mysql_to_postgresql import _mysql_rows

        manga, info = _mysql_rows(mysql_url)
        result["source_mysql"] = {"manga": len(manga), "mangainfo": len(info)}
        result["count_match"] = {
            "manga": result["manga"] == len(manga),
            "mangainfo": result["mangainfo"] == len(info),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
