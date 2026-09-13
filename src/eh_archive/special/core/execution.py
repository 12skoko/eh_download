"""Execution facilities shared by modules without a business-object dependency."""

import logging
import threading
from contextlib import contextmanager
from pathlib import Path

from .outputs import publish_json, register_output
from .repository import SpecialRepository


class ExecutionContext:
    def __init__(self, database, claim, *, config_dir, app_config):
        self.database, self.claim = database, claim
        self.config_dir, self.app_config = Path(config_dir), app_config

    @contextmanager
    def transaction(self):
        with self.database.session() as session:
            repository = SpecialRepository(session, timezone=self.app_config.timezone)
            if not repository._values(self.claim):
                raise RuntimeError("stale special execution")
            yield repository

    def progress(self, progress, *, phase=None):
        with self.transaction() as repository:
            if not repository.update_progress(self.claim, progress, phase=phase):
                raise RuntimeError("stale special progress")

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
