# Deployment

EH Archive uses one PostgreSQL database and two long-running processes:

* `eharchive-web` exposes the read/control API and never performs downloads or
  file deletion.
* `eharchive-supervisor` owns scheduling and starts bounded task subprocesses.

Create the Conda environment on each host; do not copy an environment between
Windows and Linux:

```text
mkdir -p config
cp config.sample/app.toml config.sample/supervisor.toml config.sample/crawl.toml config.sample/secrets.toml config/
```

The tracked templates live in `config.sample/`; the entire runtime `config/`
directory is ignored by Git. Edit the copied files before installing services.

```text
conda activate eh
python -m pip install -e .
eharchive --config-dir config db upgrade
eharchive --config-dir config service install
```

`service install` requires Linux, systemd, Git and root. Run it from the repository,
on the branch to deploy, with an upstream remote configured for the same branch.
It records the current Python executable and absolute paths in
`/etc/eharchive/management.toml`, creates Web, Supervisor, operation-template,
update-check service and update-check timer units, verifies them, and reloads
systemd. The daily update-check timer is enabled and started automatically.
Web and Supervisor remain stopped; add `--start` to start them immediately.

Use `eharchive service start all` or native `systemctl start eharchive-web
eharchive-supervisor`. Stop previous screen/manual processes before handing over
to systemd. Services remain stopped after a host reboot. `service repair`
regenerates units from the management configuration; `service uninstall` requires
stopped services and preserves management configuration and operation history.
Windows can run ordinary Web/Worker processes, but system management and managed
configuration publication require a Linux installation. PostgreSQL backup and
log rotation remain host administration responsibilities.

### Daily update checks

`eharchive-update-check.timer` runs a one-shot Git check daily at 06:00 in the
server's local timezone. Persistent scheduling catches up once after downtime.
It only fetches remote Git metadata; installation still requires “执行更新”.
Checks share the deployment lock with management operations. A scheduled check
skips a busy deployment and tries again at the next daily trigger. Failed checks
record the error and retry at the next trigger; manual checks remain available.

The navigation “系统” link and “Git 更新” heading show a red dot when the remote
branch contains new commits. Pages refresh local Git status every minute and
clear the dots after those commits are installed. The Git panel shows the last
check time and any check error. Browsing pages never triggers a remote fetch.

Existing installations receive both new units automatically through the Web
“执行更新” workflow (or `eharchive update apply`). After a manual Git pull, run
`eharchive service repair` as root in the activated `eh` environment.
Do not run `service install` again. Old management configuration files default
to daily checks; to change the schedule, add this to
`/etc/eharchive/management.toml` and run `eharchive service repair`:

```toml
[update_check]
enabled = true
time = "06:00" # HH:MM, server local time
```

Set `enabled = false` and repair to disable the timer. Uninstall disables and
removes both update-check units along with the other deployment units.

The fixed deployment lock is `/run/eharchive/deployment.lock`. Do not delete or
replace it while processes are running. Operation history lives under
`<app.log_dir>/management/history`; changing `log_dir` does not move the lock.
The management configuration retains old history locations so in-progress
operation links remain readable after a directory change.

Units run as root because deployment paths can be under `/root`. Configure Web
authentication before exposing the listener. The system page controls only
EH Archive units and the registered Git branch; it is not a general shell.
Verify installation, drain/cancel, stopped-service preservation and restart
health checks on the Linux deployment host before production use.

Every `roots` value and `log_dir` in `config/app.toml` must be an absolute
directory. They may be UNC paths on Windows or mounted paths on Linux; relative
paths are rejected during startup.
The database stores only root keys and safe filenames, so moving a root only
requires editing `config/app.toml`.

## Video archive special processing

Copy `config.sample/special/video_archive.toml` to
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
`download.category`; reserve it exclusively for EH Archive. A successful
combination always retains both source downloads until the Manga reaches
`completed`. Cleanup is then manually queued from Web or with
`eharchive --config-dir config special video-archive cleanup-completed`.
Supervisor never creates this cleanup job from a status change. Test the shared
APP path mapping and deletion ownership on the deployment host before using it.

Before production cutover:

1. Run the scripts in `scripts/README.md` against a read-only MySQL account.
2. Review migration, qBittorrent, LANraragi and filesystem reconciliation.
3. Start Web/Supervisor with `supervisor=paused`, then resume components one at a time.
4. Keep the old MySQL database and program read-only until a complete cycle has
   been verified.
