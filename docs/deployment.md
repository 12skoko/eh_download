# Deployment

EH Archive uses one PostgreSQL database and two long-running processes:

* `eharchive-web` exposes the read/control API and never performs downloads or
  file deletion.
* `eharchive-supervisor` owns scheduling and starts bounded task subprocesses.

Create a fresh virtual environment on every host; do not copy `.venv` between
Windows and Linux:

```text
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/eharchive --config-dir config db upgrade
```

On Windows use `.venv\Scripts\python.exe` and register the Web and Supervisor
commands with Task Scheduler, NSSM, or WinSW. On Linux use two systemd units
whose `ExecStart` values point at the same virtual environment. Both services
must use the same `config/` and PostgreSQL URL.

Every `roots` value and `log_dir` in `config/app.toml` must be an absolute
directory. They may be UNC paths on Windows or mounted paths on Linux; relative
paths are rejected during startup.
The database stores only root keys and safe filenames, so moving a root only
requires editing `config/app.toml`.

Before production cutover:

1. Run the scripts in `scripts/README.md` against a read-only MySQL account.
2. Review migration, qBittorrent, LANraragi and filesystem reconciliation.
3. Start Web/Supervisor with `all=paused`, then resume components one at a time.
4. Keep the old MySQL database and program read-only until a complete cycle has
   been verified.
