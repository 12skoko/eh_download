from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import load_config
from ..db import ArchiveRepository, Database
from ..db.models import SystemControl
from ..db.repository import utcnow
from ..domain.errors import ArchiveError, ErrorClass, classify_exception
from ..logging import (
    MAIN_LOG_ENV,
    SUPERVISOR_RUN_ID_ENV,
    configure_logging,
    get_logger,
    session_log_path,
)
from ..services.uploader.thumbnails import ThumbnailBatch

log = get_logger(__name__)


TASK_OPERATIONS = (
    "details",
    "torrent_download",
    "direct_download",
    "validate",
    "prepare",
    "upload",
    "cleanup",
    "delete",
)
SEVERE_CHILD_EXIT_CODES = {1, 2}
TEMPORARY_CHILD_EXIT_CODE = 3


class Supervisor:
    def __init__(
        self,
        database: Database,
        *,
        config_dir: str | Path = "config",
        runner_module: str = "eh_archive.tasks.runner",
        run_id: str | None = None,
        main_log_path: str | Path | None = None,
    ) -> None:
        self.database = database
        self.config_dir = str(config_dir)
        self.app, self.config, self.crawl, self.secrets = load_config(config_dir)
        self.runner_module = runner_module
        self.run_id = run_id or str(uuid.uuid4())
        self.main_log_path = Path(main_log_path).resolve() if main_log_path else None
        self.owner = f"supervisor-{self.run_id}"
        self.children: dict[str, subprocess.Popen] = {}
        self.next_start_at: dict[str, float] = {}
        self.stopping = False
        self.draining = False
        self.drain_heartbeat = True
        self.exit_code = 0
        self.failed_operations: set[str] = set()
        self.last_collect = 0.0
        self.last_thumbnails = 0.0
        self.maintenance_active = False
        self.maintenance_idle_logged = False
        self.maintenance_next_probe_at = 0.0
        self.maintenance_recovery_started_at: float | None = None
        self.timezone = ZoneInfo(self.app.timezone)
        disabled = [name for name, enabled in self.config.modules.items() if not enabled]
        if disabled:
            log.info("supervisor modules disabled by config: %s", ", ".join(disabled))

    def stop(self, *_args) -> None:
        self.stopping = True

    def tick(self) -> None:
        maintenance_blocked, heartbeat_done = self._maintenance_tick()
        if maintenance_blocked:
            return
        if self.draining:
            self._reap_children()
            if self.drain_heartbeat and not heartbeat_done:
                self._heartbeat()
            return
        if not heartbeat_done:
            self._heartbeat()
        self._reap_children()
        if self.draining:
            return
        self._complete_cancellations()
        self._maybe_collect()
        self._maybe_thumbnails()
        for operation in TASK_OPERATIONS:
            if not self.config.modules[operation] or self._paused(operation):
                continue
            if time.monotonic() < self.next_start_at.get(operation, 0.0):
                continue
            child = self.children.get(operation)
            if child is not None and child.poll() is None:
                continue
            self.children.pop(operation, None)
            with self.database.session() as session:
                if not ArchiveRepository(session).has_work(operation):
                    continue
            # One bounded child per operation. qBittorrent's own background
            # transfer count is intentionally not controlled here.
            self._start_child(
                operation,
                [
                    sys.executable,
                    "-m",
                    self.runner_module,
                    "--operation",
                    operation,
                    "--config-dir",
                    self.config_dir,
                    "--limit",
                    str(self.config.batch_size),
                ],
            )

    def _maintenance_tick(self) -> tuple[bool, bool]:
        """Return whether scheduling is blocked and whether heartbeat already ran."""

        if self.config.maintenance_start is None or self.config.maintenance_end is None:
            return False, False
        if self._in_maintenance_window():
            if not self.maintenance_active:
                self.maintenance_active = True
                self.maintenance_idle_logged = False
                self.maintenance_next_probe_at = 0.0
                self.maintenance_recovery_started_at = None
                log.info(
                    "maintenance window started: start=%s end=%s timezone=%s running_children=%s",
                    self.config.maintenance_start.isoformat(timespec="minutes"),
                    self.config.maintenance_end.isoformat(timespec="minutes"),
                    self.app.timezone,
                    ",".join(sorted(self.children)) or "none",
                )
            self._reap_children()
            if not self.children and not self.maintenance_idle_logged:
                log.info("maintenance window idle: waiting for scheduled maintenance to finish")
                self.maintenance_idle_logged = True
            return True, False
        if not self.maintenance_active:
            return False, False

        self._reap_children()
        now = time.monotonic()
        if self.maintenance_recovery_started_at is None:
            self.maintenance_recovery_started_at = now
        if now < self.maintenance_next_probe_at:
            return True, False
        try:
            self._heartbeat()
        except Exception as exc:
            info = classify_exception(exc)
            if info.code != "database_unavailable":
                raise
            elapsed = now - self.maintenance_recovery_started_at
            if elapsed >= self.config.maintenance_recovery_timeout_seconds:
                raise ArchiveError(
                    "database_unavailable",
                    "database remained unavailable for "
                    f"{self.config.maintenance_recovery_timeout_seconds:g} seconds after "
                    "the maintenance window ended",
                    ErrorClass.SYSTEM,
                ) from exc
            self.maintenance_next_probe_at = now + self.config.maintenance_retry_seconds
            log.warning(
                "maintenance window ended but database is unavailable; retrying in %s seconds "
                "elapsed=%s timeout=%s",
                self.config.maintenance_retry_seconds,
                round(elapsed, 1),
                self.config.maintenance_recovery_timeout_seconds,
            )
            return True, False

        self.maintenance_active = False
        self.maintenance_idle_logged = False
        self.maintenance_next_probe_at = 0.0
        self.maintenance_recovery_started_at = None
        log.info("maintenance window ended: database available; scheduling resumed")
        return False, True

    def _in_maintenance_window(self, now: datetime | None = None) -> bool:
        start = self.config.maintenance_start
        end = self.config.maintenance_end
        if start is None or end is None:
            return False
        local_now = (
            now.astimezone(self.timezone) if now is not None else datetime.now(self.timezone)
        )
        current = local_now.time().replace(tzinfo=None)
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def run_forever(self) -> int:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self.stop)
        try:
            while not self.stopping:
                try:
                    self.tick()
                except Exception as exc:
                    info = classify_exception(exc)
                    log.exception(
                        "supervisor tick failed; entering drain: code=%s category=%s",
                        info.code,
                        info.category.value,
                    )
                    self._enter_draining(
                        None,
                        2 if info.category == ErrorClass.SYSTEM else 1,
                        f"{info.code}: {info.message}",
                        maintain_heartbeat=False,
                    )
                if self.draining and not self.children:
                    break
                time.sleep(self.config.poll_seconds)
        finally:
            if self.stopping:
                deadline = time.monotonic() + self.config.shutdown_grace_seconds
                for child in self.children.values():
                    if child.poll() is None:
                        child.terminate()
                while self.children and time.monotonic() < deadline:
                    self._reap_children()
                    time.sleep(0.1)
            self._release_lease()
        if self.draining:
            log.critical(
                "supervisor drained and exiting: failed_operations=%s exit_code=%s",
                ",".join(sorted(self.failed_operations)) or "supervisor",
                self.exit_code,
            )
        return self.exit_code

    def _paused(self, component: str) -> bool:
        with self.database.session() as session:
            all_control = session.get(SystemControl, "all")
            own_control = session.get(SystemControl, component)
            return bool(
                (all_control and all_control.state == "paused")
                or (own_control and own_control.state == "paused")
            )

    def _heartbeat(self) -> None:
        from sqlalchemy import select

        with self.database.session() as session:
            control = session.scalar(
                select(SystemControl)
                .where(SystemControl.component == "supervisor")
                .with_for_update()
            )
            if (
                control
                and control.lease_until
                and control.lease_until > utcnow()
                and control.lease_owner not in {None, self.owner}
            ):
                raise RuntimeError("another Supervisor currently owns the lease")
            if control is None:
                control = SystemControl(
                    component="supervisor", state="running", updated_by=self.owner
                )
                session.add(control)
            control.heartbeat_at = utcnow()
            control.lease_owner = self.owner
            control.lease_until = utcnow() + timedelta(seconds=self.config.lease_seconds)

    def _reap_children(self) -> None:
        for operation, child in list(self.children.items()):
            if child.poll() is not None:
                log.info(
                    "submodule exited: operation=%s pid=%s returncode=%s",
                    operation,
                    child.pid,
                    child.returncode,
                )
                self.children.pop(operation, None)
                if operation in TASK_OPERATIONS:
                    self.next_start_at[operation] = (
                        time.monotonic() + self.config.module_restart_delay_seconds
                    )
                if self.stopping or child.returncode in (0, None):
                    continue
                if child.returncode == TEMPORARY_CHILD_EXIT_CODE:
                    log.warning(
                        "submodule ended with a temporary error: operation=%s pid=%s",
                        operation,
                        child.pid,
                    )
                    continue
                if child.returncode not in SEVERE_CHILD_EXIT_CODES:
                    log.error(
                        "submodule returned an unknown fatal code: operation=%s returncode=%s",
                        operation,
                        child.returncode,
                    )
                self._enter_draining(
                    operation,
                    int(child.returncode),
                    f"task exited with severe code {child.returncode}",
                )

    def _enter_draining(
        self,
        operation: str | None,
        returncode: int,
        reason: str,
        *,
        maintain_heartbeat: bool = True,
    ) -> None:
        first_failure = not self.draining
        self.draining = True
        self.drain_heartbeat = self.drain_heartbeat and maintain_heartbeat
        self.exit_code = 2 if returncode == 2 or self.exit_code == 2 else 1
        if operation is not None:
            self.failed_operations.add(operation)
            try:
                with self.database.session() as session:
                    ArchiveRepository(session).set_component(
                        operation,
                        "paused",
                        actor=self.owner,
                        reason=reason,
                    )
            except Exception:
                self.drain_heartbeat = False
                log.exception(
                    "failed to persist submodule pause: operation=%s reason=%s",
                    operation,
                    reason,
                )
        if first_failure:
            log.critical(
                "supervisor draining; no new submodules will start: "
                "failed_operation=%s reason=%s running_children=%s",
                operation or "supervisor",
                reason,
                ",".join(sorted(self.children)) or "none",
            )

    def _release_lease(self) -> None:
        from sqlalchemy import select

        try:
            with self.database.session() as session:
                control = session.scalar(
                    select(SystemControl)
                    .where(SystemControl.component == "supervisor")
                    .with_for_update()
                )
                if control is None or control.lease_owner != self.owner:
                    return
                control.lease_owner = None
                control.lease_until = None
                control.heartbeat_at = utcnow()
                control.updated_by = self.owner
                control.row_version += 1
        except Exception:
            log.exception("failed to release supervisor lease: owner=%s", self.owner)

    def _start_child(self, operation: str, args: list[str]) -> subprocess.Popen:
        child_env = os.environ.copy()
        if self.main_log_path is not None:
            child_env[MAIN_LOG_ENV] = str(self.main_log_path)
            child_env[SUPERVISOR_RUN_ID_ENV] = self.run_id
        child = subprocess.Popen(args, stdout=None, stderr=None, env=child_env)
        self.children[operation] = child
        log.info("submodule started: operation=%s pid=%s", operation, child.pid)
        return child

    def _maybe_collect(self) -> None:
        if not self.config.modules["collect"] or self._paused("collect"):
            return
        now = time.monotonic()
        child = self.children.get("collect")
        if child is not None and child.poll() is None:
            return
        if now - self.last_collect < self.config.collect_interval_seconds:
            return
        self._start_child(
            "collect",
            [sys.executable, "-m", "eh_archive.tasks.collect", "--config-dir", self.config_dir],
        )
        self.last_collect = now

    def _maybe_thumbnails(self) -> None:
        if not self.config.modules["thumbnail"] or self._paused("thumbnail"):
            return
        now = time.monotonic()
        child = self.children.get("thumbnail")
        if child is not None and child.poll() is None:
            return
        if now - self.last_thumbnails < self.config.thumbnail_interval_seconds:
            return
        with self.database.session() as session:
            if not ThumbnailBatch.has_work(session, limit=self.config.batch_size):
                self.last_thumbnails = now
                return
        self._start_child(
            "thumbnail",
            [
                sys.executable,
                "-m",
                "eh_archive.tasks.thumbnails",
                "--config-dir",
                self.config_dir,
                "--limit",
                str(self.config.batch_size),
            ],
        )
        self.last_thumbnails = now

    def _complete_cancellations(self) -> None:
        with self.database.session() as session:
            ArchiveRepository(session).complete_cancellations(limit=self.config.batch_size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-supervisor")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    app, _, _, _ = load_config(args.config_dir)
    run_id = str(uuid.uuid4())
    requested_log_path = session_log_path(
        app.log_dir, "supervisor", timezone=app.timezone, run_id=run_id
    )
    main_log_path = configure_logging(
        app.log_level,
        app.log_dir,
        timezone=app.timezone,
        component="supervisor",
        run_id=run_id,
        log_file=requested_log_path,
    )
    log.info(
        "supervisor started: run_id=%s pid=%s log=%s",
        run_id,
        os.getpid(),
        main_log_path,
    )
    try:
        exit_code = Supervisor(
            Database(app.database_url),
            config_dir=args.config_dir,
            run_id=run_id,
            main_log_path=main_log_path,
        ).run_forever()
    finally:
        log.info("supervisor stopped: run_id=%s pid=%s", run_id, os.getpid())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
