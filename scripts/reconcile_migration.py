from __future__ import annotations

import argparse
import json

from eh_archive.db import Database
from eh_archive.db.models import MangaRecord
from eh_archive.services.paths import ArtifactPathService
try:
    from scripts.migration_config import load_migration_config
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from migration_config import load_migration_config


def reconcile(database: Database, config_dir: str = "config") -> dict:
    app, _, _, _ = __import__("eh_archive.config", fromlist=["load_config"]).load_config(config_dir)
    paths = ArtifactPathService(app)
    with database.session() as session:
        rows = list(session.query(MangaRecord))
        missing = []
        invalid = []
        for row in rows:
            if not row.artifact_filename:
                continue
            if not row.artifact_location:
                invalid.append({"manga_id": row.manga_id, "reason": "missing_location"})
                continue
            try:
                path = (
                    paths.torrent_registered(row.manga_id, row.artifact_filename)
                    if row.artifact_location == "torrent_download"
                    else paths.validate_registered(row.artifact_location, row.artifact_filename)
                )
                if not path.exists():
                    missing.append(row.manga_id)
            except (OSError, ValueError) as exc:
                invalid.append({"manga_id": row.manga_id, "reason": str(exc)})
        return {
            "records": len(rows),
            "needs_manual_review": [row.manga_id for row in rows if row.status == "manual_review"],
            "missing_artifacts": missing,
            "invalid_artifacts": invalid,
        }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="migration TOML file containing PostgreSQL settings")
    parser.add_argument("--postgres", help="target PostgreSQL URL (alternative to --config)")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    if args.config:
        if args.postgres:
            parser.error("--config cannot be combined with --postgres")
        try:
            _mysql_url, postgres_url = load_migration_config(args.config)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.postgres:
        postgres_url = args.postgres
    else:
        parser.error("provide --config or --postgres")
    print(
        json.dumps(
            reconcile(Database(postgres_url), args.config_dir), ensure_ascii=False, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
