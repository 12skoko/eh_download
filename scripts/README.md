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

## Collect requeue by run ID

Every automatic or CLI `collect` run creates an auditable `run_id`. The run
covers all configured URLs, pagination, and the final `screenall` pass. The
ID is stored only on `event_log`; no snapshot or operation table is created.

The CLI prints the ID after a successful manual collect. For Supervisor runs,
read it from the collect log or query recent start events:

```sql
SELECT run_id, created_at, actor, detail
FROM event_log
WHERE event_type = 'collect_started'
ORDER BY created_at DESC;
```

Preview a rollback first:

```powershell
python scripts/rollback_operation.py `
  --config-dir config `
  --run-id <run_id> `
  --database-only `
  --dry-run `
  --report .\rollback-<run_id>.json
```

Apply after reviewing the report:

```powershell
python scripts/rollback_operation.py `
  --config-dir config `
  --run-id <run_id> `
  --database-only `
  --apply
```

This is a collect-only requeue, not a field-level database rollback. Records
touched or status-changed by the run are reset from `discovered`,
`download_pending`, or `skipped` to `deferred`. The next automatic collect may
crawl farther because these rows participate in the dynamic end calculation.
Rows with a later job attempt, lease, download ID, artifact, or LANraragi ID
block the operation. The command never calls qBittorrent or LANraragi and
never changes local files.
