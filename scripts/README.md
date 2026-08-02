# One-time migration tools

These scripts are intentionally outside `src/eh_archive`. They are the only
place that knows the legacy MySQL columns and `state`/`autostate` values.

1. Copy `config/migration.sample.toml` to `config/migration.toml` and fill in
   the structured MySQL/PostgreSQL settings. Passwords are not part of a URL,
   so characters such as `/` and `@` do not need URL encoding.
2. Run `migrate_mysql_to_postgresql.py --config config/migration.toml --dry-run`
   and review the JSON report.
3. Run it with `--apply` against a fresh PostgreSQL database.
4. Run `verify_migration.py` and then `reconcile_migration.py` before enabling
   Supervisor. The scripts never delete old MySQL rows or remote archives.

Install the optional MySQL dependency with `python -m pip install -e ".[migration]"`.
