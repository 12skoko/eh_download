from __future__ import annotations

import argparse
import uuid
from collections import defaultdict
from pathlib import Path

from ..config import load_config
from ..db import ArchiveRepository, Database
from ..domain.errors import ErrorClass, classify_exception
from ..logging import RunReport, clean_report_value, configure_logging, get_logger
from ..services.screening import ScreenDecision, ScreeningService

log = get_logger("screen")


def _write_report(report: RunReport, decisions: list[ScreenDecision]) -> None:
    report.section("screen")
    grouped: dict[str, list[ScreenDecision]] = defaultdict(list)
    singles: list[ScreenDecision] = []
    for decision in decisions:
        if decision.screen_group_id:
            grouped[decision.screen_group_id].append(decision)
        else:
            singles.append(decision)

    for decision in singles:
        report.write(
            f"{decision.resulting_status}: {clean_report_value(decision.manga_id)} | "
            f"{clean_report_value(decision.real_name)} | reason={decision.reason}"
        )
    if singles and grouped:
        report.write("")

    for group_id, group in grouped.items():
        first = group[0]
        report.write(
            f"group: {group_id} | {clean_report_value(first.real_name)} | "
            f"candidates={first.candidate_count}"
        )
        for decision in group:
            priority = "" if decision.priority is None else f" | priority={decision.priority:.6f}"
            report.write(
                f"{decision.resulting_status}: {clean_report_value(decision.manga_id)} | "
                f"reason={decision.reason}{priority}"
            )
        report.write("")

    statuses: dict[str, int] = defaultdict(int)
    for decision in decisions:
        statuses[decision.resulting_status] += 1
    report.finish({"status": "succeeded", "processed": len(decisions), **dict(statuses)})


def run(config_dir: str | Path = "config", *, limit: int | None = None) -> int:
    app, supervisor, crawl, _ = load_config(config_dir)
    run_id = str(uuid.uuid4())
    configure_logging(
        app.log_level,
        app.log_dir,
        timezone=app.timezone,
        component="screen",
        run_id=run_id,
    )
    batch_limit = limit if limit is not None else supervisor.batch_size_for("screen")
    report = RunReport(app.log_dir, "screen", timezone=app.timezone, run_id=run_id)
    report.fields({"batch_limit": batch_limit})
    try:
        with Database(app.database_url).session() as session:
            result = ScreeningService(
                ArchiveRepository(session, run_id=run_id),
                crawl,
            ).run_batch(batch_limit)
    except Exception as exc:
        error = classify_exception(exc)
        report.fatal(exc, result={"processed": 0})
        log.exception("screen submodule failed: run_id=%s", run_id)
        return 2 if error.category == ErrorClass.SYSTEM else 1
    _write_report(report, result.decisions)
    log.info(
        "screen completed: run_id=%s processed=%s queued=%s filtered_out=%s skipped=%s",
        run_id,
        result.processed,
        result.queued,
        result.filtered_out,
        result.skipped,
    )
    return 2 if report.write_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-screen")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    return run(args.config_dir, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
