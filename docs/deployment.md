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

The repository currently provides deployment guidance only; it does not ship
ready-to-install systemd units, Windows service definitions, PostgreSQL
backup/restore automation, or built-in log rotation. Configure these with the
host operating system and test them before production cutover.

Every `roots` value and `log_dir` in `config/app.toml` must be an absolute
directory. They may be UNC paths on Windows or mounted paths on Linux; relative
paths are rejected during startup.
The database stores only root keys and safe filenames, so moving a root only
requires editing `config/app.toml`.

## Video archive special processing

Copy `config/special/video_archive.sample.toml` to
`config/special/video_archive.toml` and create the configured workspace.
The module reuses `app.qbit_torrent_path` as the path qBittorrent sees and
`app.roots.torrent_download` as the local/mounted path that reaches the same
files. `work.workspace_root` must be a separate writable directory that does
not overlap `roots.torrent_download`. Configure an
absolute ffmpeg executable that provides the `libwebp` encoder. Secrets remain
in the existing `secrets.toml`; never copy qBittorrent credentials or EH
cookies into the module file.

Web and Supervisor read only the module's declarative enabled configuration when
offering or scheduling the extension. They do not periodically probe ffmpeg or
the video workspace. The manually requested compose worker verifies local
content, workspace writability/free-space access, and the configured ffmpeg
`libwebp` encoder after both Torrents report complete.

Run `eharchive --config-dir config db upgrade` while Web and Supervisor are
stopped or drained. Revision `0013_special_processing` adds
`special_processing`, `special_workflow`, and `special_job`. Back up PostgreSQL
before applying it. Restart both Web and Supervisor after changing the module
configuration or the `[special_processing]` Supervisor settings.

The module uses the exact qBittorrent category configured in
`download.category`; reserve it exclusively for EH Archive. The default
`cleanup_source_on_success=false` retains both source downloads after a
successful combination for manual recovery. Set it to true only after testing
the shared APP path mapping and deletion ownership on the deployment host.

Before production cutover:

1. Run the scripts in `scripts/README.md` against a read-only MySQL account.
2. Review migration, qBittorrent, LANraragi and filesystem reconciliation.
3. Start Web/Supervisor with `supervisor=paused`, then resume components one at a time.
4. Keep the old MySQL database and program read-only until a complete cycle has
   been verified.
