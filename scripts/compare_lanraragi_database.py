from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from eh_archive.config import load_config
from eh_archive.db import Database, MangaRecord

if __package__:
    from .collect_all_archives import fetch_archives, output_path, write_json
else:
    from collect_all_archives import fetch_archives, output_path, write_json


GALLERY_URL = re.compile(
    r"https?://(?:www\.)?(?:exhentai|e-hentai)\.org/g/(\d+)/",
    re.IGNORECASE,
)


def numeric_database_id(manga_id: str) -> int | None:
    value = manga_id.partition("/")[0]
    return int(value) if value.isdigit() else None


def numeric_archive_id(archive: dict[str, Any]) -> int | None:
    tags = archive.get("tags")
    if not isinstance(tags, str):
        return None
    match = GALLERY_URL.search(tags.replace("\\", ""))
    return int(match.group(1)) if match else None


def _sorted_ids(values: Iterable[int]) -> list[int]:
    return sorted(values)


def build_comparison(
    database_manga_ids: Iterable[str], archives: list[dict[str, Any]]
) -> dict[str, Any]:
    database_ids: list[int] = []
    invalid_database_ids: list[str] = []
    for manga_id in database_manga_ids:
        numeric_id = numeric_database_id(manga_id)
        if numeric_id is None:
            invalid_database_ids.append(manga_id)
        else:
            database_ids.append(numeric_id)

    lanraragi_ids: list[int] = []
    unparsed_archives: list[dict[str, str]] = []
    for archive in archives:
        numeric_id = numeric_archive_id(archive)
        if numeric_id is None:
            unparsed_archives.append(
                {
                    "arcid": str(archive.get("arcid", "")),
                    "title": str(archive.get("title", "")),
                }
            )
        else:
            lanraragi_ids.append(numeric_id)

    database_counts = Counter(database_ids)
    lanraragi_counts = Counter(lanraragi_ids)
    database_set = set(database_counts)
    lanraragi_set = set(lanraragi_counts)
    database_only = _sorted_ids(database_set - lanraragi_set)
    lanraragi_only = _sorted_ids(lanraragi_set - database_set)
    return {
        "summary": {
            "database_completed_rows": len(database_ids) + len(invalid_database_ids),
            "database_resolved_ids": len(database_ids),
            "database_unique_ids": len(database_set),
            "lanraragi_archives": len(archives),
            "lanraragi_resolved_ids": len(lanraragi_ids),
            "lanraragi_unique_ids": len(lanraragi_set),
            "database_only": len(database_only),
            "lanraragi_only": len(lanraragi_only),
            "unparsed_lanraragi_archives": len(unparsed_archives),
        },
        "database_only": database_only,
        "lanraragi_only": lanraragi_only,
        "database_duplicate_ids": {
            str(value): count for value, count in sorted(database_counts.items()) if count > 1
        },
        "lanraragi_duplicate_ids": {
            str(value): count for value, count in sorted(lanraragi_counts.items()) if count > 1
        },
        "invalid_database_manga_ids": sorted(invalid_database_ids),
        "unparsed_lanraragi_archives": unparsed_archives,
    }


def read_archives(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("archive JSON must contain the list returned by LANraragi /api/archives")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare completed manga in PostgreSQL with LANraragi archives."
    )
    parser.add_argument("--config-dir", default="config")
    parser.add_argument(
        "--archives",
        help="read a previous all_archives JSON export instead of requesting LANraragi",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    app, _, _, secrets = load_config(args.config_dir)
    started = datetime.now(ZoneInfo(app.timezone))
    if args.archives:
        archives = read_archives(args.archives)
        source = str(Path(args.archives).resolve())
    else:
        archives = fetch_archives(
            app.lanraragi_url,
            headers=secrets.lanraragi,
            timeout=args.timeout,
        )
        source = f"{app.lanraragi_url.rstrip('/')}/api/archives"

    database = Database(app.database_url)
    try:
        with database.session() as session:
            database_manga_ids = list(
                session.scalars(
                    select(MangaRecord.manga_id).where(MangaRecord.status == "completed")
                )
            )
    finally:
        database.dispose()

    comparison = build_comparison(database_manga_ids, archives)
    finished = datetime.now(ZoneInfo(app.timezone))
    report = {
        "generated_at": finished.isoformat(),
        "database_status": "completed",
        "lanraragi_source": source,
        "elapsed_seconds": round((finished - started).total_seconds(), 3),
        **comparison,
    }
    path = output_path(app.log_dir, "lanraragi_database_comparison", app.timezone)
    write_json(path, report)

    summary = report["summary"]
    print(f"Database completed rows: {summary['database_completed_rows']}")
    print(f"LANraragi archives: {summary['lanraragi_archives']}")
    print(f"Database only: {summary['database_only']}")
    print(f"LANraragi only: {summary['lanraragi_only']}")
    print(f"Unparsed LANraragi archives: {summary['unparsed_lanraragi_archives']}")
    print(f"JSON written: {path.resolve()}")
    print(f"Elapsed: {report['elapsed_seconds']:.2f} seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
