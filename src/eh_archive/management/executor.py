from __future__ import annotations

import argparse
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import requests
from sqlalchemy import select

from ..config import load_config
from ..db import Database
from ..db.models import SystemControl
from . import ManagementError
from .config import (
    DEFAULT_CONFIG,
    SUPERVISOR_UNIT,
    WEB_UNIT,
    load_management_config,
    write_management_config,
)
from .configuration import publish, restore
from .git import GitRepository
from .installer import live_url
from .lock import deployment_lock
from .state import (
    KINDS,
    TERMINAL,
    OperationStore,
    event,
    now,
    read_json,
    update_state,
)
from .systemd import CommandRunner, Systemd


class Cancelled(ManagementError):
    pass


class Executor:
    def __init__(self, identifier: str, management_path: Path = DEFAULT_CONFIG):
        self.management_path = Path(management_path)
        self.config = load_management_config(self.management_path)
        self.path = OperationStore(self.config).locate(identifier)
        self.runner = CommandRunner(self.path / "operation.log")
        self.systemd = Systemd(self.runner)
        self._state_lock = threading.RLock()
        self._done = threading.Event()
        self._database = None
        self.previous = {}

    @property
    def database(self):
        if self._database is None:
            app, _, _, _ = load_config(self.config.config_dir)
            self._database = Database(app.database_url)
        return self._database

    def state(self, **changes):
        with self._state_lock:
            return update_state(self.path, **changes)

    def step(self, phase, callback, *args, **kwargs):
        self.state(phase=phase)
        event(self.path, phase, status="started")
        try:
            result = callback(*args, **kwargs)
        except Exception as exc:
            event(self.path, phase, status="failed", error=str(exc))
            if not isinstance(exc, Cancelled):
                state = read_json(self.path / "state.json")
                if not state.get("failed_phase"):
                    self.state(failed_phase=phase, original_error=str(exc))
            raise
        event(self.path, phase, status="completed")
        return result

    def _heartbeat(self):
        while not self._done.wait(5):
            self.state()

    def run(self) -> int:
        try:
            with deployment_lock(blocking=True):
                # Resolve the submitted operation before locking, but reload deployment paths now.
                self.config = load_management_config(self.management_path)
                if read_json(self.path / "state.json")["status"] in TERMINAL:
                    return (
                        0
                        if read_json(self.path / "state.json")["status"]
                        in {
                            "succeeded",
                            "cancelled",
                        }
                        else 1
                    )
                self._recover_interrupted()
                request = read_json(self.path / "request.json")
                if request["kind"] not in KINDS:
                    raise ManagementError("Invalid operation kind", "invalid_request")
                self.state(status="running", started_at=now(), phase="acquire_lock")
                thread = threading.Thread(target=self._heartbeat, daemon=True)
                thread.start()
                try:
                    kind = request["kind"]
                    if kind == "git_update":
                        self.update()
                    elif kind == "apply_config":
                        self.apply_config()
                    else:
                        self.control_services(kind)
                    self.state(
                        status="succeeded",
                        phase="completed",
                        finished_at=now(),
                        result="Operation completed",
                    )
                    return 0
                except Cancelled as exc:
                    self.state(status="cancelled", error=str(exc), finished_at=now())
                    return 0
                except Exception as exc:  # noqa: BLE001 - persist all executor failures
                    self.runner.log(f"FAILED: {type(exc).__name__}: {exc}")
                    self.state(
                        status="failed",
                        error_code=getattr(exc, "code", "operation_failed"),
                        error=str(exc),
                        finished_at=now(),
                    )
                    event(self.path, "failed", error=str(exc))
                    self._diagnostics()
                    return 1
                finally:
                    self._done.set()
                    thread.join()
        except Exception as exc:  # noqa: BLE001 - process-level failure boundary
            self.runner.log(f"Executor failed: {exc}")
            self.state(
                status="failed",
                error_code=getattr(exc, "code", "executor_failed"),
                error=str(exc),
                finished_at=now(),
            )
            return 1

    def _recover_interrupted(self):
        store = OperationStore(self.config)
        for state in store.list():
            if state["id"] == self.path.name or state["status"] in TERMINAL:
                continue
            if state["status"] == "pending":
                continue
            if self.systemd.active(self.systemd.operation_unit(state["id"])):
                raise ManagementError("Previous executor is still active", "operation_conflict")
            old_path = store.locate(state["id"])
            if state["kind"] == "apply_config" and (old_path / "configuration.json").exists():
                restore(self.config, old_path)
            update_state(
                old_path,
                status="interrupted",
                finished_at=now(),
                error_code="executor_lost",
                error="Previous executor exited unexpectedly",
            )
            if state["kind"] == "git_update":
                raise ManagementError(
                    "Interrupted update requires manual inspection", "manual_recovery_required"
                )

    def _diagnostics(self):
        for unit in (WEB_UNIT, SUPERVISOR_UNIT):
            try:
                self.runner.log(f"{unit}: {self.systemd.status(unit)}")
                self.runner.run(["journalctl", "-u", unit, "-n", "40", "--no-pager"], check=False)
            except (ManagementError, OSError) as exc:
                self.runner.log(f"Cannot collect diagnostics: {exc}")

    def control(self, state=None) -> str:
        with self.database.session() as session:
            row = session.scalar(
                select(SystemControl)
                .where(SystemControl.component == "supervisor")
                .with_for_update()
            )
            previous = row.state if row else "running"
            if state is not None:
                if row is None:
                    row = SystemControl(
                        component="supervisor",
                        state=state,
                        updated_by=f"operation:{self.path.name}",
                    )
                    session.add(row)
                else:
                    row.state = state
                    row.reason = f"management operation {self.path.name}"
                    row.updated_by = f"operation:{self.path.name}"
                    row.row_version += 1
            return previous

    def capture(self, units):
        self.previous = {unit: self.systemd.active(unit) for unit in units}
        if SUPERVISOR_UNIT in units:
            control = self.control()
            if control == "draining":
                raise ManagementError("Supervisor is already draining", "operation_conflict")
            self.previous["control"] = control
        self.state(previous_services=self.previous)

    def wait(self, predicate, timeout, message):
        start = time.monotonic()
        while not predicate():
            if timeout and time.monotonic() - start >= timeout:
                raise ManagementError(message, "health_timeout")
            time.sleep(self.config.poll_seconds)

    def drain(self):
        if not self.systemd.active(SUPERVISOR_UNIT):
            return
        self.step("request_draining", self.control, "draining")

        def stopped():
            if (self.path / "cancel.json").exists():
                # Serialize against Supervisor's transaction that consumes a completed drain.
                with self.database.session() as session:
                    row = session.scalar(
                        select(SystemControl)
                        .where(SystemControl.component == "supervisor")
                        .with_for_update()
                    )
                    accepted = row is not None and row.state == "draining"
                    if accepted:
                        row.state = self.previous.get("control", "running")
                        row.updated_by = f"operation:{self.path.name}"
                        row.reason = "management drain cancelled"
                        row.row_version += 1
                if accepted:
                    raise Cancelled("Drain cancelled")
                event(self.path, "cancel", status="rejected", reason="Drain already completed")
                (self.path / "cancel.json").unlink(missing_ok=True)
            status = self.systemd.status(SUPERVISOR_UNIT)
            if status["ActiveState"] == "failed" or status.get("Result", "success") != "success":
                raise ManagementError("Supervisor failed during drain", "drain_failed")
            return status["ActiveState"] == "inactive"

        try:
            self.step(
                "wait_for_supervisor_exit",
                self.wait,
                stopped,
                self.config.supervisor_drain_timeout_seconds,
                "Drain timed out",
            )
        except Cancelled:
            raise
        except Exception:
            self.control(self.previous.get("control", "running"))
            raise

    def stop(self, unit):
        if unit == SUPERVISOR_UNIT:
            self.drain()
        else:
            self.step("stop_web", self.systemd.command, "stop", unit)

    def start(self, unit):
        started = datetime.now(UTC)
        if unit == SUPERVISOR_UNIT:
            self.control("paused")
        self.step(
            "start_" + ("web" if unit == WEB_UNIT else "supervisor"),
            self.systemd.command,
            "start",
            unit,
        )
        if unit == WEB_UNIT:
            app, _, _, _ = load_config(self.config.config_dir)

            def healthy():
                try:
                    response = requests.get(
                        live_url(app),
                        timeout=3,
                        allow_redirects=False,
                        proxies={"http": "", "https": ""},
                    )
                    return (
                        self.systemd.active(unit)
                        and response.status_code == 200
                        and response.json().get("ok") is True
                    )
                except (requests.RequestException, ValueError):
                    return False

            self.step(
                "wait_for_web_health",
                self.wait,
                healthy,
                self.config.web_start_timeout_seconds,
                "Web did not become healthy",
            )
        else:

            def heartbeat():
                with self.database.session() as session:
                    row = session.get(SystemControl, "supervisor")
                    stamp = row.heartbeat_at if row else None
                    if stamp and stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=UTC)
                    return bool(
                        stamp and stamp >= started and row.lease_owner and self.systemd.active(unit)
                    )

            self.step(
                "wait_for_supervisor_heartbeat",
                self.wait,
                heartbeat,
                self.config.supervisor_start_timeout_seconds,
                "Supervisor did not publish a fresh heartbeat",
            )
            self.step(
                "restore_previous_control_state",
                self.control,
                self.previous.get("control", "running"),
            )
            started = datetime.now(UTC)
            self.step(
                "verify_supervisor_heartbeat",
                self.wait,
                heartbeat,
                self.config.supervisor_start_timeout_seconds,
                "Supervisor heartbeat stopped after restoring control state",
            )

    def restore_services(self):
        for unit in (WEB_UNIT, SUPERVISOR_UNIT):
            if self.previous.get(unit):
                self.start(unit)
            else:
                event(self.path, f"health:{unit}", status="not_applicable")

    def control_services(self, kind):
        action, target = kind.split("_", 1)
        units = (
            [SUPERVISOR_UNIT, WEB_UNIT]
            if target == "all"
            else [WEB_UNIT if target == "web" else SUPERVISOR_UNIT]
        )
        self.step("capture_service_state", self.capture, units)
        try:
            if action in {"stop", "restart"}:
                for unit in units:
                    if self.previous[unit]:
                        self.stop(unit)
            if action in {"start", "restart"}:
                for unit in reversed(units):
                    if action == "restart" or not self.previous[unit]:
                        self.start(unit)
        finally:
            if SUPERVISOR_UNIT in units:
                self.control(self.previous["control"])

    def apply_config(self):
        metadata = read_json(self.path / "configuration.json")
        scope = metadata["scope"]
        units = {
            "next_worker": [],
            "supervisor": [SUPERVISOR_UNIT],
            "web": [WEB_UNIT],
            "web_and_supervisor": [SUPERVISOR_UNIT, WEB_UNIT],
        }[scope]
        self.step("capture_service_state", self.capture, units)
        try:
            # Drain before publishing so a cancelled operation leaves live configuration untouched.
            for unit in units:
                if self.previous[unit]:
                    self.stop(unit)
            self.step("publish_configuration", publish, self.config, self.path)
            self._database = None
            self.restore_services()
            app, _, _, _ = load_config(self.config.config_dir)
            log_dir = Path(app.log_dir)
            if not log_dir.is_absolute():
                log_dir = self.config.repository / log_dir
            new_config = replace(
                self.config,
                management_dir=log_dir / "management",
                history_dirs=self.config.roots,
                web_live_url=live_url(app),
            )
            self.step(
                "publish_management_configuration",
                write_management_config,
                new_config,
                self.management_path,
            )
        except Cancelled:
            raise
        except Exception:
            self.step("restore_configuration", restore, self.config, self.path)
            self._database = None
            for unit in units:
                if self.previous[unit] and self.systemd.active(unit):
                    self.stop(unit)
            self.restore_services()
            raise
        finally:
            if SUPERVISOR_UNIT in units:
                self.control(self.previous["control"])

    def update(self):
        git = GitRepository(self.config, self.runner)
        info = self.step("verify_repository", git.inspect, fetch=True, require_clean=True)
        self.state(**info)
        self.step("capture_service_state", self.capture, [SUPERVISOR_UNIT, WEB_UNIT])
        from alembic.migration import MigrationContext

        with self.database.engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_heads()
        self.state(database_revision=list(revision))
        migration_started = False
        configuration_started = False
        code_changed = False
        try:
            if self.previous[SUPERVISOR_UNIT]:
                self.drain()
            if self.previous[WEB_UNIT]:
                self.stop(WEB_UNIT)
            # Revalidate after a potentially long drain before changing tracked files.
            current = self.step("revalidate_repository", git.inspect, require_clean=True)
            if current["old_commit"] != info["old_commit"]:
                raise ManagementError("Repository changed while draining", "repository_changed")
            code_changed = True
            self.step("fast_forward_checkout", git.checkout, info["target_commit"])
            self.step("sync_python_environment", self.sync_environment)
            configuration_started = True
            self.step("merge_configuration", self.merge_configuration)
            self.step("validate_configuration", self.validate_configuration)
            migration_started = True
            self.step(
                "upgrade_database",
                self.runner.run,
                [
                    self.config.python,
                    "-m",
                    "eh_archive.cli",
                    "--config-dir",
                    self.config.config_dir,
                    "db",
                    "upgrade",
                ],
                cwd=self.config.repository,
                timeout=None,
            )
            self.step(
                "refresh_systemd_units",
                self.runner.run,
                [
                    self.config.python,
                    "-m",
                    "eh_archive.management.installer",
                    "--management-config",
                    self.management_path,
                ],
                cwd=self.config.repository,
            )
            self._database = None
            self.restore_services()
            self.state(deployed_commit=info["target_commit"])
        except Cancelled:
            raise
        except Exception:
            if not migration_started and not configuration_started:
                if code_changed:
                    self.step("restore_old_commit", git.restore, info["old_commit"])
                    self.step("restore_old_environment", self.sync_environment)
                self.restore_services()
            raise
        finally:
            # Merging/migration failures require manual inspection, not recovery writes.
            if not migration_started and not configuration_started:
                self.control(self.previous["control"])

    def merge_configuration(self):
        backup = (
            self.config.config_dir
            / "backups"
            / datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S-%f")
        )
        self.state(configuration_backup=str(backup))
        # Use the newly checked-out code, not modules already imported by this executor.
        self.runner.run(
            [
                self.config.python,
                "-m",
                "eh_archive.management.config_sync",
                self.config.config_dir,
                "--samples",
                self.config.repository / "config.sample",
                "--backup",
                backup,
            ],
            cwd=self.config.repository,
        )

    def sync_environment(self):
        self.runner.run(
            [self.config.python, "-m", "pip", "install", "-e", self.config.repository],
            cwd=self.config.repository,
            timeout=None,
        )

    def validate_configuration(self):
        self.runner.run(
            [
                self.config.python,
                "-m",
                "eh_archive.management.validate",
                str(self.config.config_dir),
            ],
            cwd=self.config.repository,
        )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--management-config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    return Executor(args.operation_id, args.management_config).run()


if __name__ == "__main__":
    raise SystemExit(main())
