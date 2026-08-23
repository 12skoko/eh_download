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

## LANraragi/database comparison

Export the complete LANraragi archive list:

```powershell
python scripts/collect_all_archives.py --config-dir config
```

Compare the current LANraragi archive list with new-database rows whose status
is `completed`:

```powershell
python scripts/compare_lanraragi_database.py --config-dir config
```

The comparison can instead use a previously exported file:

```powershell
python scripts/compare_lanraragi_database.py `
  --config-dir config `
  --archives <path-to-all_archives-json>
```

Both scripts read the LANraragi URL and authentication headers from the normal
application configuration. Generated JSON is always written below the
configured `log_dir/tools` directory, which should remain outside version
control. The comparison lists database-only IDs, LANraragi-only IDs, duplicate
IDs, invalid database IDs, and LANraragi archives whose source gallery URL
could not be parsed.

## Download artifact cleanup

`cleanup_download_artifacts.py` scans the configured torrent, direct, H@H, and
aria2 download roots. It also scans every qBittorrent task whose category is
exactly `eharchive`. A recognized item is eligible only when the matching
PostgreSQL manga row is `completed` or `deleted` and has no active attempt or
lease. The script never changes PostgreSQL and never calls LANraragi.

Preview all eligible and skipped items first:

```powershell
python scripts/cleanup_download_artifacts.py --config-dir config
```

Test one numeric gallery ID before a full cleanup:

```powershell
python scripts/cleanup_download_artifacts.py --config-dir config --id 4127104
python scripts/cleanup_download_artifacts.py --config-dir config --id 4127104 --apply
```

Apply the full cleanup only after reviewing the dry-run JSON report:

```powershell
python scripts/cleanup_download_artifacts.py --config-dir config --apply
```

qBittorrent tasks are removed first with `delete_files=false`; the script then
removes the corresponding numeric torrent directory. Direct and aria2 ZIP
files are removed individually, while recognized H@H gallery directories are
removed recursively. Unrecognized names, temporary files, symlinks, missing
database rows, non-terminal states, and anomalous active leases are preserved
and listed in the report.
