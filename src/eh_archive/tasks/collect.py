from __future__ import annotations

import argparse
import uuid
from collections import defaultdict
from pathlib import Path

from ..config import load_config
from ..db import ArchiveRepository, Database, ScreenDecision
from ..domain.errors import EH_SITE_UNAVAILABLE_EXIT_CODE, ErrorClass, classify_exception
from ..logging import RunReport, clean_report_value, configure_logging, get_logger
from ..services.collector import CollectionResult, Collector

log = get_logger(__name__)


def _collect_item_line(item) -> str:
    fields = [
        f"{item.action}: {clean_report_value(item.manga_id)}",
        clean_report_value(item.name),
        f"category={clean_report_value(item.category)}",
        f"status={item.status}",
    ]
    if item.screen_pending:
        fields.append("screen_pending=true")
    if item.remark and not item.screen_pending:
        fields.append(f"reason={clean_report_value(item.remark)}")
    return " | ".join(fields)


def _write_collect_report(
    report: RunReport,
    source_results: list[tuple[str, CollectionResult]],
    decisions: list[ScreenDecision],
    *,
    screened: int,
) -> None:
    report.section("crawl")
    combined = CollectionResult()
    for source_index, (url, result) in enumerate(source_results, 1):
        report.write(f"source [{source_index}/{len(source_results)}]: {clean_report_value(url)}")
        for page_index, page in enumerate(result.pages, 1):
            report.write(
                f"page [{page_index}]: {clean_report_value(page.url)} | "
                f"found={page.discovered}, created={page.created}, updated={page.updated}, "
                f"errors={page.errors}"
            )
            for item in page.items:
                report.write(_collect_item_line(item))
        report.write("")
        combined.add(result)

    report.write(
        "crawl summary: "
        f"pages={len(combined.pages)}, found={combined.discovered}, "
        f"created={sum(item.action == 'created' for item in combined.items)}, "
        f"updated={sum(item.action == 'updated' for item in combined.items)}, "
        f"queued={combined.queued}, "
        f"screen_pending={sum(item.screen_pending for item in combined.items)}, "
        f"deferred={combined.deferred}, "
        "excluded="
        f"{sum(item.remark in {'excluded_category', 'screen_not_eligible'} for item in combined.items)}, "
        f"errors={combined.errors}"
    )

    report.section("screen")
    grouped: dict[str, list[ScreenDecision]] = defaultdict(list)
    for decision in decisions:
        grouped[decision.screen_group_id].append(decision)
    for group in grouped.values():
        first = group[0]
        report.write(
            f"group: {first.screen_group_id} | {clean_report_value(first.real_name)} | "
            f"candidates={first.candidate_count}"
        )
        for decision in group:
            outcome = "selected" if decision.selected else "rejected"
            report.write(
                f"{outcome}: {clean_report_value(decision.manga_id)} | "
                f"status={decision.resulting_status} | priority={decision.priority:.6f}"
            )
        report.write("")
    report.write(
        f"screen summary: groups={len(grouped)}, processed={screened}, "
        f"selected={sum(decision.selected for decision in decisions)}, "
        f"rejected={sum(not decision.selected for decision in decisions)}"
    )

    report.finish(
        {
            "status": "succeeded",
            "queued_total": combined.queued + sum(decision.selected for decision in decisions),
        }
    )


def run(config_dir: str | Path = "config", *, end: int | None = None) -> int:
    app, _, crawl, secrets = load_config(config_dir)
    run_id = str(uuid.uuid4())
    configure_logging(
        app.log_level,
        app.log_dir,
        timezone=app.timezone,
        component="collect",
        run_id=run_id,
    )
    database = Database(app.database_url)
    report = RunReport(app.log_dir, "collect", timezone=app.timezone, run_id=run_id)
    report.fields({"config_dir": config_dir, "sources": len(crawl.collection_urls())})
    collect_end: int | None = None
    screened = 0
    collection_urls = crawl.collection_urls()
    source_results: list[tuple[str, CollectionResult]] = []
    screen_decisions: list[ScreenDecision] = []
    current_source: str | None = None
    try:
        with database.session() as session:
            repository = ArchiveRepository(session)
            collect_end = (
                end
                if end is not None
                else repository.automatic_collect_end(
                    days=crawl.collect_end_days, offset=crawl.collect_end_offset
                )
            )
            report.write(
                "boundary: "
                f"end={collect_end}, collect_end_days={crawl.collect_end_days}, "
                f"collect_end_offset={crawl.collect_end_offset}"
            )
            repository.start_collect_run(
                trigger_source="supervisor",
                run_id=run_id,
                detail={
                    "config_dir": str(config_dir),
                    "end": collect_end,
                    "urls": list(collection_urls),
                    "observation_days": crawl.observation_days,
                    "collect_end_days": crawl.collect_end_days,
                    "collect_end_offset": crawl.collect_end_offset,
                    "name_keywords": list(crawl.name_keywords),
                    "tag_keywords": list(crawl.tag_keywords),
                    "exclude_categories": list(crawl.exclude_categories),
                },
            )
            log.info(
                "automatic collection started: run_id=%s end=%s url_count=%s",
                run_id,
                collect_end,
                len(collection_urls),
            )
            log.info("automatic collection boundary: end=%s run_id=%s", collect_end, run_id)
            collector = Collector(repository, app, crawl, secrets)
            for url in collection_urls:
                current_source = url
                result = collector.collect_url(
                    url, source="automatic", actor="collector", end=collect_end
                )
                source_results.append((url, result))
            screened = repository.screenall(decisions=screen_decisions)
            log.info("screenall completed: %s rows resolved run_id=%s", screened, run_id)
            repository.finish_collect_run("succeeded", detail={"end": collect_end})
    except Exception as exc:
        error = classify_exception(exc)
        report.fatal(
            exc,
            context={"current_source": current_source or "not_started", "database": "rolled_back"},
            result={"sources_completed": len(source_results)},
        )
        log.exception("automatic collection failed: run_id=%s", run_id)
        if error.code == "eh_site_unavailable":
            return EH_SITE_UNAVAILABLE_EXIT_CODE
        return (
            2
            if error.category == ErrorClass.SYSTEM
            else 3
            if error.category == ErrorClass.TEMPORARY
            else 1
        )

    _write_collect_report(report, source_results, screen_decisions, screened=screened)
    log.info(
        "automatic collection finished: run_id=%s end=%s screened=%s status=succeeded",
        run_id,
        collect_end,
        screened,
    )
    return 2 if report.write_failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-collect")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--end", type=int)
    args = parser.parse_args(argv)
    return run(args.config_dir, end=args.end)


if __name__ == "__main__":
    raise SystemExit(main())
