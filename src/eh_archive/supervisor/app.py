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
from ..domain.errors import (
    EH_SITE_UNAVAILABLE_EXIT_CODE,
    ArchiveError,
    ErrorClass,
    classify_exception,
)
from ..logging import (
    MAIN_LOG_ENV,
    SUPERVISOR_RUN_ID_ENV,
    configure_logging,
    get_logger,
    session_log_path,
    special_job_log_path,
)
from ..management.config_migrations import migrate_configuration
from ..special import SpecialRepository
from ..special.handlers import enabled_module_capabilities
from ..tasks.registry import MODULES
from .scheduling import ACTIVE_REQUESTS, INTERVAL_MODULES, aware

log = get_logger(__name__)


TASK_OPERATIONS = tuple(MODULES)
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
        # Ownership identifies this process, even when a log run_id is reused.
        self.owner = f"supervisor-{uuid.uuid4()}"
        self.children: dict[str, subprocess.Popen] = {}
        self.next_start_at: dict[str, float] = {}
        self.eh_cooldowns: dict[str, tuple[float, datetime]] = {}
        self.stopping = False
        self.draining = False
        self.drain_heartbeat = True
        self.control_state: str | None = None
        self._last_graceful_children: tuple[str, ...] | None = None
        self.exit_code = 0
        self.failed_operations: set[str] = set()
        now = time.monotonic()
        self.next_run_at = {
            name: now + self.config.schedule_for(name).initial_delay_seconds
            for name, module in MODULES.items()
            if module.schedule == "interval"
        }
        self.last_health_check = 0.0
        self.next_special_start_at = 0.0
        self.health_checks_enabled = True
        self.maintenance_active = False
        self.maintenance_idle_logged = False
        self.maintenance_next_probe_at = 0.0
        self.maintenance_recovery_started_at: float | None = None
        self.timezone = ZoneInfo(self.app.timezone)
        disabled = [name for name, enabled in self.config.modules.items() if not enabled]
        if disabled:
            log.info("supervisor modules disabled by config: %s", ", ".join(disabled))
        self.special_enabled_kinds: tuple[str, ...] = ()
        self.special_concurrency_limit = 0
        self.special_module_limits = {}
        if self.config.special_processing_enabled and self.config.modules.get(
            "special_processing", True
        ):
            capabilities = enabled_module_capabilities(config_dir)
            self.special_enabled_kinds = tuple(item.kind for item in capabilities)
            if capabilities:
                self.special_concurrency_limit = self.config.special_max_concurrency
                self.special_module_limits = {
                    item.kind: item.max_concurrency for item in capabilities
                }

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
        self._maybe_refresh_health()
        self._reap_children()
        if self.draining:
            return
        if self.control_state == "draining":
            self._log_graceful_drain_wait()
            return
        if self.control_state == "paused":
            return
        self._complete_cancellations()
        self._maybe_special_jobs()
        for operation in MODULES:
            self._maybe_module(operation)

    def _maybe_module(self, operation: str) -> None:
        module = MODULES[operation]
        if not self.config.modules[operation] or self._paused(operation):
            return
        now = time.monotonic()
        if now < self.next_start_at.get(operation, 0.0):
            return
        child = self.children.get(operation)
        if child is not None and child.poll() is None:
            return
        if module.schedule == "interval":
            manual = self._prepare_interval_start(operation)
            if manual is None:
                return
        else:
            with self.database.session() as session:
                if not ArchiveRepository(session).has_work(operation):
                    return
        try:
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
                ],
            )
        except OSError:
            if module.schedule == "interval":
                self._finish_interval_start(operation, manual, failed=True)
            raise
        if module.schedule == "interval":
            self.next_run_at[operation] = (
                time.monotonic() + self.config.schedule_for(operation).interval_seconds
            )
            self._finish_interval_start(operation, manual)

    def _interval_block_reason(self, operation, control):
        if not self.config.modules[operation]:
            return "模块已禁用"
        if self.draining or self.control_state == "draining":
            return "Supervisor 正在停止调度"
        if self.control_state == "paused":
            return "Supervisor 已暂停"
        if control.state == "paused":
            return "模块已暂停"
        if self.maintenance_active or self._in_maintenance_window():
            return "维护期间，暂不可执行"
        if time.monotonic() < self.next_start_at.get(operation, 0.0):
            return "等待重试冷却结束"
        return None

    def _publish_interval(self, session, operation):
        control = session.get(SystemControl, operation, with_for_update=True)
        if control is None:
            control = SystemControl(component=operation, state="running", updated_by=self.owner)
            session.add(control)
        if control.trigger_status in ACTIVE_REQUESTS and control.trigger_owner != self.owner:
            control.trigger_status = "expired"
            control.trigger_message = "请求已失效：Supervisor 已重启，请重新触发"
        child = self.children.get(operation)
        control.schedule_running = child is not None and child.poll() is None
        control.schedule_block_reason = self._interval_block_reason(operation, control)
        if control.trigger_status == "pending" and (
            control.schedule_block_reason or control.schedule_running
        ):
            control.trigger_status = "rejected"
            control.trigger_message = "请求未执行：" + (
                control.schedule_block_reason or "模块已在运行"
            )
        control.lease_owner = self.owner
        control.next_run_at = utcnow() + timedelta(seconds=(
            max(self.next_run_at[operation], self.next_start_at.get(operation, 0.0))
            - time.monotonic()
        ))
        control.schedule_updated_at = utcnow()
        return control

    def _prepare_interval_start(self, operation):
        """Claim before Popen; an interrupted claim is never automatically replayed."""
        with self.database.session() as session:
            supervisor = session.get(SystemControl, "supervisor", with_for_update=True)
            if (
                supervisor is None or supervisor.lease_owner != self.owner
                or not supervisor.lease_until or aware(supervisor.lease_until) <= utcnow()
                or supervisor.state != "running"
            ):
                return None
            control = self._publish_interval(session, operation)
            if control.schedule_running or control.schedule_block_reason:
                return None
            manual = control.trigger_status == "pending" and control.trigger_owner == self.owner
            if not manual and time.monotonic() < self.next_run_at[operation]:
                return None
            if control.trigger_status == "starting":
                return None
            # Reserve the start before releasing the lock, including timer starts.
            # This closes the window in which Web could queue a second run.
            control.schedule_running = True
            if manual:
                control.trigger_status = "starting"
                control.trigger_message = "Supervisor 已接收，正在启动"
            return manual

    def _finish_interval_start(self, operation, manual, *, failed=False):
        with self.database.session() as session:
            supervisor = session.get(SystemControl, "supervisor", with_for_update=True)
            if supervisor is None or supervisor.lease_owner != self.owner:
                return
            control = self._publish_interval(session, operation)
            if manual:
                control.trigger_status = "failed" if failed else "started"
                control.trigger_message = (
                    "启动失败，请查看 Supervisor 日志" if failed else "已启动（不代表执行完成）"
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
        graceful_drain_completed = False
        try:
            while not self.stopping:
                try:
                    self.tick()
                    if (
                        not self.draining
                        and self.control_state == "draining"
                        and not self.children
                        and self._complete_graceful_drain()
                    ):
                        graceful_drain_completed = True
                        break
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
        elif graceful_drain_completed:
            log.info("graceful drain completed; supervisor stopped with exit_code=0")
        return self.exit_code

    def _paused(self, component: str) -> bool:
        with self.database.session() as session:
            own_control = session.get(SystemControl, component)
            return bool(own_control and own_control.state == "paused")

    def _maybe_refresh_health(self) -> None:
        if not getattr(self, "health_checks_enabled", False):
            return
        now = time.monotonic()
        if now - self.last_health_check < self.config.health_check_interval_seconds:
            return
        from .health import refresh_health_snapshots

        try:
            results = refresh_health_snapshots(
                self.database,
                self.app,
                self.config,
                self.secrets,
            )
        except Exception:
            log.exception("failed to persist health snapshots")
            return
        self.last_health_check = now
        unavailable = [result.component for result in results if result.status == "unavailable"]
        if unavailable:
            log.warning("health checks unavailable: components=%s", ",".join(unavailable))

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
                and aware(control.lease_until) > utcnow()
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
            self._set_control_state(control.state)
            self._publish_cooldowns(session)
            for operation in INTERVAL_MODULES:
                self._publish_interval(session, operation)

    def _publish_cooldowns(self, session) -> None:
        # Memory remains authoritative: a new Supervisor clears old snapshots.
        cooldowns = getattr(self, "eh_cooldowns", {})
        now = time.monotonic()
        for operation in (*MODULES, "special_processing"):
            row = session.get(SystemControl, operation, with_for_update=True)
            if row is None:
                row = SystemControl(component=operation, state="running")
                session.add(row)
            active = cooldowns.get(operation)
            release = bool(
                active and row.cooldown_release_requested
                and row.cooldown_owner == self.owner and row.cooldown_until
                and aware(row.cooldown_until) == active[1]
            )
            if active and (release or now >= active[0]):
                if release:
                    if operation == "special_processing":
                        self.next_special_start_at = 0.0
                    else:
                        self.next_start_at[operation] = 0.0
                    log.info("EH cooldown released: operation=%s", operation)
                cooldowns.pop(operation, None)
                active = None
            row.cooldown_until = active[1] if active else None
            row.cooldown_reason = "E-H 站点不可用" if active else None
            row.cooldown_owner = self.owner
            row.cooldown_release_requested = False

    def _set_control_state(self, state: str) -> None:
        previous = self.control_state
        self.control_state = state
        if state == previous:
            return
        self._last_graceful_children = None
        if state == "paused":
            log.info("supervisor paused; no new submodules will start")
        elif state == "draining":
            log.info(
                "graceful drain requested; no new submodules will start: running_children=%s",
                ",".join(sorted(self.children)) or "none",
            )
        elif previous == "draining":
            log.info("graceful drain cancelled; supervisor scheduling resumed")
        elif previous == "paused":
            log.info("supervisor scheduling resumed")

    def _log_graceful_drain_wait(self) -> None:
        running = tuple(sorted(self.children))
        if not running or running == self._last_graceful_children:
            return
        self._last_graceful_children = running
        log.info("graceful drain waiting: running_children=%s", ",".join(running))

    def _complete_graceful_drain(self) -> bool:
        """Atomically consume a drain request after every child has exited."""

        from sqlalchemy import select

        with self.database.session() as session:
            control = session.scalar(
                select(SystemControl)
                .where(SystemControl.component == "supervisor")
                .with_for_update()
            )
            if control is None or control.lease_owner != self.owner:
                raise RuntimeError("Supervisor lease was lost while completing graceful drain")
            if control.state != "draining":
                self._set_control_state(control.state)
                return False
            control.state = "paused"
            control.reason = "graceful drain completed"
            control.updated_by = self.owner
            control.row_version += 1
            self.control_state = "paused"
        return True

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
                special_child = operation.startswith("special:")
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
                if child.returncode == EH_SITE_UNAVAILABLE_EXIT_CODE:
                    cooldown = self.app.eh_unavailable_cooldown_seconds
                    affected_operation = "special_processing" if special_child else operation
                    if cooldown == 0:
                        if special_child:
                            self.next_special_start_at = max(
                                self.next_special_start_at,
                                time.monotonic() + self.config.module_restart_delay_seconds,
                            )
                            log.error(
                                "special module site access failed while cooldown is disabled; "
                                "ordinary Supervisor scheduling continues"
                            )
                            continue
                        self._enter_draining(
                            affected_operation,
                            2,
                            "E-Hentai/ExHentai unavailable and module cooldown is disabled",
                        )
                        continue
                    cooldown_until = time.monotonic() + cooldown
                    if not hasattr(self, "eh_cooldowns"):
                        self.eh_cooldowns = {}
                    self.eh_cooldowns[affected_operation] = (
                        cooldown_until, utcnow() + timedelta(seconds=cooldown)
                    )
                    if special_child:
                        self.next_special_start_at = max(
                            self.next_special_start_at,
                            cooldown_until,
                        )
                    else:
                        self.next_start_at[operation] = cooldown_until
                    log.warning(
                        "E-Hentai/ExHentai unavailable; submodule cooling down: "
                        "operation=%s cooldown_seconds=%s; other modules continue",
                        affected_operation,
                        cooldown,
                    )
                    continue
                if special_child:
                    # Extension failures are confined to their persisted job.
                    # The worker records the failure for retry/exit in Web; an
                    # optional module must never drain the ordinary pipeline.
                    self.next_special_start_at = max(
                        self.next_special_start_at,
                        time.monotonic() + self.config.module_restart_delay_seconds,
                    )
                    log.error(
                        "special worker failed without draining Supervisor: "
                        "operation=%s returncode=%s",
                        operation,
                        child.returncode,
                    )
                    continue
                if child.returncode not in SEVERE_CHILD_EXIT_CODES:
                    log.error(
                        "submodule returned an unknown fatal code: operation=%s returncode=%s",
                        operation,
                        child.returncode,
                    )
                self._enter_draining(
                    "special_processing" if special_child else operation,
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

    def _maybe_special_jobs(self) -> None:
        enabled_kinds = getattr(self, "special_enabled_kinds", ())
        if not enabled_kinds or self._paused("special_processing"):
            return
        now = time.monotonic()
        if now < self.next_special_start_at:
            return
        running = sum(
            1
            for key, child in self.children.items()
            if key.startswith("special:") and child.poll() is None
        )
        capacity = getattr(self, "special_concurrency_limit", 0) - running
        while capacity > 0:
            with self.database.session() as session:
                claim = SpecialRepository(
                    session, run_id=self.run_id, timezone=self.app.timezone
                ).claim_next(
                    owner=self.owner,
                    lease_seconds=self.config.special_job_lease_seconds,
                    enabled_kinds=enabled_kinds,
                    max_concurrency=self.special_concurrency_limit,
                    module_limits=getattr(self, "special_module_limits", {}),
                )
            if claim is None:
                break
            key = f"special:{claim.job_id}"
            try:
                self._start_child(
                    key,
                    [
                        sys.executable,
                        "-m",
                        "eh_archive.special.worker",
                        "--job-id",
                        str(claim.job_id),
                        "--workflow-id",
                        str(claim.workflow_id),
                        "--kind",
                        claim.kind,
                        "--lease-token",
                        claim.lease_token,
                        "--lease-owner",
                        claim.lease_owner,
                        "--config-dir",
                        self.config_dir,
                        "--run-id",
                        self.run_id,
                        "--log-path",
                        str(
                            special_job_log_path(
                                self.app.log_dir,
                                claim.kind,
                                workflow_id=claim.workflow_id,
                                job_id=claim.job_id,
                                timezone=self.app.timezone,
                                run_id=self.run_id,
                            )
                        ),
                    ],
                )
            except OSError as exc:
                with self.database.session() as session:
                    SpecialRepository(session, run_id=self.run_id, timezone=self.app.timezone).fail(
                        claim,
                        error_code="special_worker_start_failed",
                        error_detail=str(exc),
                    )
                log.exception("failed to start special worker: job_id=%s", claim.job_id)
                break
            capacity -= 1
        self.next_special_start_at = now + self.config.special_processing_poll_seconds

    def _complete_cancellations(self) -> None:
        with self.database.session() as session:
            ArchiveRepository(session).complete_cancellations(limit=self.config.batch_size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-supervisor")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    migrated_files = migrate_configuration(args.config_dir)
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
    if migrated_files:
        log.info("配置迁移完成：%s", ", ".join(migrated_files))
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
