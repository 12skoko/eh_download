from __future__ import annotations

import argparse
from pathlib import Path

from ..config import load_config
from ..db import ArchiveRepository, Database
from ..logging import configure_logging, get_logger
from ..services.collector import Collector

log = get_logger(__name__)


def run(config_dir: str | Path = "config", *, end: int | None = None) -> int:
    app, _, crawl, secrets = load_config(config_dir)
    configure_logging(app.log_level, app.log_dir)
    database = Database(app.database_url)
    with database.session() as session:
        repository = ArchiveRepository(session)
        collect_end = (
            end
            if end is not None
            else repository.automatic_collect_end(
                days=crawl.collect_end_days, offset=crawl.collect_end_offset
            )
        )
        run_id = repository.start_collect_run(
            trigger_source="supervisor",
            detail={
                "config_dir": str(config_dir),
                "end": collect_end,
                "urls": list(crawl.collection_urls()),
                "observation_days": crawl.observation_days,
                "collect_end_days": crawl.collect_end_days,
                "collect_end_offset": crawl.collect_end_offset,
                "name_keywords": list(crawl.name_keywords),
                "tag_keywords": list(crawl.tag_keywords),
                "exclude_categories": list(crawl.exclude_categories),
            },
        )
        log.info("automatic collection boundary: end=%s run_id=%s", collect_end, run_id)
        collector = Collector(repository, app, crawl, secrets)
        for url in crawl.collection_urls():
            collector.collect_url(url, source="automatic", actor="collector", end=collect_end)
        screened = repository.screenall()
        log.info("screenall completed: %s rows resolved run_id=%s", screened, run_id)
        repository.finish_collect_run("succeeded", detail={"end": collect_end})
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-collect")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--end", type=int)
    args = parser.parse_args(argv)
    return run(args.config_dir, end=args.end)


if __name__ == "__main__":
    raise SystemExit(main())
