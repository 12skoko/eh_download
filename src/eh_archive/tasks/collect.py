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
        log.info("automatic collection boundary: end=%s", collect_end)
        collector = Collector(repository, app, crawl, secrets)
        for url in crawl.urls.values():
            collector.collect_url(url, source="automatic", actor="collector", end=collect_end)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-collect")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--end", type=int)
    args = parser.parse_args(argv)
    return run(args.config_dir, end=args.end)


if __name__ == "__main__":
    raise SystemExit(main())
