from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

from ..config import load_config
from ..db import ArchiveRepository, Database
from ..db.models import JobAttempt, MangaRecord, SystemControl
from ..db.repository import utcnow
from ..domain.states import Status
from ..logging import configure_logging, get_logger
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


class Supervisor:
    def __init__(
        self,
        database: Database,
        *,
        config_dir: str | Path = "config",
        runner_module: str = "eh_archive.tasks.runner",
    ) -> None:
        self.database = database
        self.config_dir = str(config_dir)
        self.app, self.config, self.crawl, self.secrets = load_config(config_dir)
        self.runner_module = runner_module
        self.owner = f"supervisor-{uuid.uuid4()}"
        self.children: dict[str, subprocess.Popen] = {}
        self.stopping = False
        self.last_collect = 0.0
        self.last_thumbnails = 0.0
        disabled = [name for name, enabled in self.config.modules.items() if not enabled]
        if disabled:
            log.info("supervisor modules disabled by config: %s", ", ".join(disabled))

    def stop(self, *_args) -> None:
        self.stopping = True

    def tick(self) -> None:
        self._heartbeat()
        self._reap_children()
        self._recover_expired()
        self._complete_cancellations()
        self._maybe_collect()
        self._maybe_thumbnails()
        for operation in TASK_OPERATIONS:
            if not self.config.modules[operation] or self._paused(operation):
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
            self.children[operation] = subprocess.Popen(
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
                stdout=None,
                stderr=None,
            )

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self.stop)
        while not self.stopping:
            try:
                self.tick()
            except RuntimeError as exc:
                log.error("supervisor lease unavailable: %s", exc)
                self.stopping = True
            except Exception:
                log.exception("supervisor tick failed")
                time.sleep(min(self.config.poll_seconds * 2, 60))
            else:
                time.sleep(self.config.poll_seconds)
        deadline = time.monotonic() + self.config.shutdown_grace_seconds
        for child in self.children.values():
            if child.poll() is None:
                child.terminate()
        while self.children and time.monotonic() < deadline:
            self._reap_children()
            time.sleep(0.1)

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
                if child.returncode not in (0, None):
                    with self.database.session() as session:
                        ArchiveRepository(session).set_component(
                            operation,
                            "paused",
                            actor=self.owner,
                            reason=f"task exited with code {child.returncode}",
                        )
                self.children.pop(operation, None)

    def _maybe_collect(self) -> None:
        if not self.config.modules["collect"] or self._paused("collect"):
            return
        now = time.monotonic()
        child = self.children.get("collect")
        if child is not None and child.poll() is None:
            return
        if now - self.last_collect < self.config.collect_interval_seconds:
            return
        self.children["collect"] = subprocess.Popen(
            [sys.executable, "-m", "eh_archive.tasks.collect", "--config-dir", self.config_dir],
            stdout=None,
            stderr=None,
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
        self.children["thumbnail"] = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "eh_archive.tasks.thumbnails",
                "--config-dir",
                self.config_dir,
                "--limit",
                str(self.config.batch_size),
            ],
            stdout=None,
            stderr=None,
        )
        self.last_thumbnails = now

    def _recover_expired(self) -> None:
        now = utcnow()
        with self.database.session() as session:
            rows = list(session.scalars(select_expired(now)))
            for manga in rows:
                if manga.active_attempt_id:
                    attempt = session.get(JobAttempt, manga.active_attempt_id)
                    if attempt and attempt.status == "running":
                        attempt.status, attempt.finished_at = "abandoned", now
                target = {
                    Status.DOWNLOADING.value: Status.DOWNLOAD_PENDING.value,
                    Status.VALIDATING.value: Status.DOWNLOADED.value,
                    Status.PREPARING.value: Status.DOWNLOADED.value,
                    Status.UPLOADING.value: Status.UPLOAD_PENDING.value,
                }.get(manga.status, manga.status)
                manga.status = target
                manga.status_updated_at = manga.updated_at = now
                manga.active_attempt_id = manga.lease_token = manga.lease_owner = (
                    manga.lease_until
                ) = None
                manga.next_retry_at = now
                manga.row_version += 1

    def _complete_cancellations(self) -> None:
        with self.database.session() as session:
            ArchiveRepository(session).complete_cancellations(limit=self.config.batch_size)


def select_expired(now):
    from sqlalchemy import select

    return select(MangaRecord).where(MangaRecord.lease_until < now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-supervisor")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    app, _, _, _ = load_config(args.config_dir)
    configure_logging(app.log_level, app.log_dir)
    Supervisor(Database(app.database_url), config_dir=args.config_dir).run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
