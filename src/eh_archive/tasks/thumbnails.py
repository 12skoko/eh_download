from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from ..config import load_config
from ..db import Database
from ..domain.errors import ErrorClass, classify_exception
from ..logging import (
    RunReport,
    clean_report_value,
    configure_logging,
    get_logger,
)
from ..services.uploader.lanraragi import LANraragiClient
from ..services.uploader.thumbnails import ThumbnailBatch, ThumbnailBatchResult

log = get_logger(__name__)


def _write_report(report: RunReport, result: ThumbnailBatchResult) -> None:
    report.section("thumbnails")
    total = len(result.items)
    for index, item in enumerate(result.items, 1):
        fields = [
            f"[{index}/{total}] {clean_report_value(item.manga_id)}",
            item.outcome,
            f"archive_id={clean_report_value(item.archive_id)}",
        ]
        if item.error_code:
            fields.append(f"error={clean_report_value(item.error_code)}")
        if item.detail:
            fields.append(f"detail={clean_report_value(item.detail)[:500]}")
        report.write(" | ".join(fields))
    status = "succeeded_with_errors" if result.failed else "succeeded"
    report.finish(
        {
            "attempted": result.attempted,
            "regenerated": result.accepted,
            "failed": result.failed,
            "skipped": result.skipped,
            "status": status,
        }
    )


def run(config_dir: str | Path = "config", *, limit: int = 100, run_id: str | None = None) -> int:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    app, _, _, secrets = load_config(config_dir)
    run_id = run_id or str(uuid.uuid4())
    report = RunReport(app.log_dir, "thumbnail", timezone=app.timezone, run_id=run_id)
    report.fields({"batch_limit": limit})
    database = Database(app.database_url)
    client = LANraragiClient(
        app.lanraragi_url,
        headers=secrets.lanraragi,
    )
    try:
        with database.session() as session:
            result = ThumbnailBatch(session, client, run_id=run_id).run(limit=limit)
    except Exception as exc:
        report.fatal(exc)
        log.exception("thumbnail submodule failed: run_id=%s", run_id)
        return 2 if classify_exception(exc).category == ErrorClass.SYSTEM else 1
    _write_report(report, result)
    return 2 if report.write_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-thumbnails")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    app, _, _, _ = load_config(args.config_dir)
    run_id = str(uuid.uuid4())
    configure_logging(
        app.log_level,
        app.log_dir,
        timezone=app.timezone,
        component="thumbnail",
        run_id=run_id,
    )
    return run(args.config_dir, limit=args.limit, run_id=run_id)


if __name__ == "__main__":
    raise SystemExit(main())
