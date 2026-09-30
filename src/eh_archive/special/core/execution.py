"""Execution facilities shared by modules without a business-object dependency."""

import logging
import math
import threading
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text

from ...db.models import SystemControl
from .outputs import publish_json, register_output
from .registry import get_workflow_definition
from .repository import SpecialRepository


class ExecutionContext:
    def __init__(self, database, claim, *, config_dir, app_config):
        self.database, self.claim = database, claim
        self.config_dir, self.app_config = Path(config_dir), app_config

    @contextmanager
    def transaction(self, *, timeout_seconds=None):
        with self.database.session() as session:
            if timeout_seconds is not None and session.bind.dialect.name == "postgresql":
                if timeout_seconds <= 0:
                    raise ValueError("transaction timeout must be positive")
                session.execute(
                    text("SELECT set_config('lock_timeout', :timeout, true), "
                         "set_config('statement_timeout', :timeout, true)"),
                    {"timeout": f"{math.ceil(timeout_seconds * 1000)}ms"},
                )
            repository = SpecialRepository(session, timezone=self.app_config.timezone)
            if not repository._values(self.claim):
                raise RuntimeError("stale special execution")
            yield repository

    def progress(self, progress, *, phase=None):
        with self.transaction() as repository:
            if not repository.update_progress(self.claim, progress, phase=phase):
                raise RuntimeError("stale special progress")

    def stop_requested(self):
        """Return a stop reason; a revoked claim raises before further I/O."""
        policy = get_workflow_definition(self.claim.kind).lifecycle
        if policy is None or not policy.cooperative_stop:
            return None
        with self.transaction() as repository:
            job, _ = repository._values(self.claim)
            control = (job.progress or {}).get("_execution_control", {})
            if control.get("stop_reason"):
                return str(control["stop_reason"])
            supervisor = repository.session.get(SystemControl, "supervisor")
            if supervisor and supervisor.state == "draining":
                return "supervisor_draining"
        return None

    def publish(self, output_id, data, *, name="report.json"):
        entry = publish_json(
            Path(self.app_config.log_dir) / "special_outputs",
            self.claim,
            output_id,
            data,
            name=name,
        )
        with self.transaction() as repository:
            if not register_output(repository, self.claim, entry):
                raise RuntimeError("stale special output")
        return entry


@contextmanager
def keep_lease(database, claim, *, lease_seconds):
    """Heartbeat only renews the job lease; it never advances module phases."""
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(min(30, max(1, lease_seconds / 3))):
            try:
                with database.session() as session:
                    if not SpecialRepository(session).renew(claim, lease_seconds=lease_seconds):
                        return
            except Exception:
                logging.getLogger(__name__).exception(
                    "special heartbeat failed: job=%s", claim.job_id
                )
                return

    thread = threading.Thread(target=heartbeat, name=f"special-lease-{claim.job_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)
    # Each database mutation is independently fenced even if heartbeat fails.
    # Don't turn a committed result into a failure because the terminal job
    # correctly refused a concurrent heartbeat.
