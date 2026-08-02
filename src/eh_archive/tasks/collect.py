from __future__ import annotations

import argparse
from pathlib import Path

from ..config import load_config
from ..db import ArchiveRepository, Database
from ..logging import configure_logging
from ..services.collector import Collector


def run(config_dir: str | Path = "config") -> int:
    app, _, crawl, secrets = load_config(config_dir)
    configure_logging(app.log_level, app.log_dir)
    database = Database(app.database_url)
    with database.session() as session:
        collector = Collector(ArchiveRepository(session), app, crawl, secrets)
        for url in crawl.urls.values():
            collector.collect_url(url, source="automatic", actor="collector")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-collect")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    return run(args.config_dir)


if __name__ == "__main__":
    raise SystemExit(main())
