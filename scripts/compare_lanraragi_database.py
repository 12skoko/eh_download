from __future__ import annotations

import argparse
import json
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


from eh_archive.special.modules.lanraragi_compare.comparison import (
    GALLERY_URL,
    build_comparison,
    numeric_archive_id,
    numeric_database_id,
)


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

__all__ = [
    "GALLERY_URL",
    "build_comparison",
    "main",
    "numeric_archive_id",
    "numeric_database_id",
    "read_archives",
]
