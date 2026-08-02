from __future__ import annotations

import argparse
from pathlib import Path

from ..config import load_config
from ..db import Database
from ..logging import configure_logging
from ..services.uploader.lanraragi import LANraragiClient
from ..services.uploader.thumbnails import ThumbnailBatch


def run(config_dir: str | Path = "config", *, limit: int = 100) -> int:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    app, _, _, secrets = load_config(config_dir)
    database = Database(app.database_url)
    client = LANraragiClient(
        app.lanraragi_url,
        headers=secrets.lanraragi,
    )
    with database.session() as session:
        ThumbnailBatch(session, client).run(limit=limit)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-thumbnails")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    app, _, _, _ = load_config(args.config_dir)
    configure_logging(app.log_level, app.log_dir)
    return run(args.config_dir, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
