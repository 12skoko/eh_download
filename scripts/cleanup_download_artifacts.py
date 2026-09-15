"""Command-line entry point for download artifact cleanup."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from eh_archive.config import load_config
from eh_archive.db import Database
from eh_archive.integrations.qbittorrent import QBittorrentClient
from eh_archive.special.modules.download_cleanup.cleanup import reconcile


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
