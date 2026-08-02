# One-time migration tools

These scripts are intentionally outside `src/eh_archive`. They are the only
place that knows the legacy MySQL columns and `state`/`autostate` values.

1. Run `migrate_mysql_to_postgresql.py --dry-run` and review the JSON report.
2. Run it with `--apply` against a fresh PostgreSQL database.
3. Run `verify_migration.py` and then `reconcile_migration.py` before enabling
   Supervisor. The scripts never delete old MySQL rows or remote archives.

Install the optional MySQL dependency with `pip install -e .[migration]`.
