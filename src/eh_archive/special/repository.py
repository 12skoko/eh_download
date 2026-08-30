from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from ..db.models import EventLog, MangaRecord, SpecialJob, SpecialWorkflow
from ..db.repository import utcnow
from ..domain.states import Status
from ..services.validator.artifact import ArtifactFingerprint
from .registry import CLEANUP_SOURCES_AFTER_COMPLETE, get_operation
from .remarks import restore_entry_error, sync_remark


@dataclass(frozen=True)
class ClaimedSpecialJob:
    job_id: int
    workflow_id: int
    manga_id: str
    kind: str
    operation: str
    lease_token: str
    lease_owner: str
    workflow_version: int
    artifact_generation: int | None


class SpecialCancellationRequested(RuntimeError):
    """Raised when the active worker reaches a safe cancellation point."""


class SpecialRepository:
    def __init__(self, session: Session, *, run_id: str | None = None, timezone: str = "UTC"):
        self.session = session
        self.run_id = run_id
        self.timezone = timezone

    def active_for_manga(self, manga_id: str) -> SpecialWorkflow | None:
        return self.session.scalar(
            select(SpecialWorkflow)
            .where(
                SpecialWorkflow.manga_id == manga_id,
                SpecialWorkflow.status == "active",
            )
            .order_by(SpecialWorkflow.created_at.desc())
        )

    def queue_job(
        self,
        workflow: SpecialWorkflow,
        operation: str,
        *,
        trigger_source: str,
        requested_by: str,
        next_run_at: datetime | None = None,
    ) -> tuple[SpecialJob, bool]:
        get_operation(workflow.kind, operation)
        if operation == CLEANUP_SOURCES_AFTER_COMPLETE:
            if workflow.status != "completed" or workflow.phase != "ready":
                raise ValueError("completed source cleanup requires a ready workflow")
            manga = self.session.get(MangaRecord, workflow.manga_id)
            if manga is None or manga.status != Status.COMPLETED.value:
                raise ValueError("completed source cleanup requires a completed manga")
        elif workflow.status != "active":
            raise ValueError("special workflow is not active")
        existing = self.session.scalar(
            select(SpecialJob)
            .where(
                SpecialJob.workflow_id == workflow.id,
                SpecialJob.operation == operation,
                SpecialJob.status.in_(("queued", "running")),
            )
            .order_by(SpecialJob.created_at.desc())
        )
        if existing is not None:
            return existing, False
        attempt_no = self.session.scalar(
            select(func.coalesce(func.max(SpecialJob.attempt_no), 0) + 1).where(
                SpecialJob.workflow_id == workflow.id,
                SpecialJob.operation == operation,
            )
        )
        job = SpecialJob(
            workflow_id=workflow.id,
            operation=operation,
            status="queued",
            trigger_source=trigger_source,
            requested_by=requested_by,
            attempt_no=int(attempt_no or 1),
            next_run_at=next_run_at or utcnow(),
            progress={},
        )
        self.session.add(job)
        self.session.flush()
        self._event(
            workflow,
            "special_job_queued",
            operation=operation,
            actor=requested_by,
            detail={"job_id": job.id, "trigger_source": trigger_source},
        )
        return job, True

    def has_queued(self, *, enabled_kinds: Iterable[str]) -> bool:
        now = utcnow()
        return (
            self.session.scalar(
                select(SpecialJob.id)
                .join(SpecialWorkflow, SpecialWorkflow.id == SpecialJob.workflow_id)
                .join(MangaRecord, MangaRecord.manga_id == SpecialWorkflow.manga_id)
                .where(
                    SpecialJob.status == "queued",
                    SpecialJob.next_run_at <= now,
                    SpecialJob.lease_until.is_(None),
                    SpecialWorkflow.kind.in_(tuple(enabled_kinds)),
                    self._claimable_state(),
                )
                .limit(1)
            )
            is not None
        )

    def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int,
        enabled_kinds: Iterable[str],
    ) -> ClaimedSpecialJob | None:
        now = utcnow()
        job = self.session.scalar(
            select(SpecialJob)
            .join(SpecialWorkflow, SpecialWorkflow.id == SpecialJob.workflow_id)
            .join(MangaRecord, MangaRecord.manga_id == SpecialWorkflow.manga_id)
            .where(
                SpecialJob.status == "queued",
                SpecialJob.next_run_at <= now,
                SpecialJob.lease_until.is_(None),
                SpecialWorkflow.kind.in_(tuple(enabled_kinds)),
                self._claimable_state(),
            )
            .order_by(SpecialJob.next_run_at, SpecialJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        workflow = self.session.get(SpecialWorkflow, job.workflow_id)
        manga = self.session.get(MangaRecord, workflow.manga_id) if workflow else None
        if workflow is None or manga is None:
            return None
        operation = get_operation(workflow.kind, job.operation)
        if workflow.phase not in operation.allowed_phases:
            detail = f"operation {job.operation} cannot run in phase {workflow.phase}"
            job.status = "failed"
            job.error_code = "invalid_special_phase"
            job.error_detail = detail
            job.finished_at = now
            workflow.phase = "failed"
            workflow.error_code = job.error_code
            workflow.error_detail = detail
            payload = dict(workflow.payload or {})
            payload["retry_operation"] = job.operation
            workflow.payload = payload
            workflow.progress = {"message": "failed"}
            workflow.row_version += 1
            workflow.updated_at = now
            manga.last_error_operation = "special_processing"
            manga.last_error_code = job.error_code
            manga.last_error_detail = detail
            manga.last_error_at = now
            manga.updated_at = now
            manga.row_version += 1
            sync_remark(manga, workflow, timezone=self.timezone)
            self._event(
                workflow,
                "special_job_failed",
                operation=job.operation,
                actor=owner,
                error_code=job.error_code,
                detail={"job_id": job.id, "phase": workflow.phase},
            )
            return None
        token = str(uuid.uuid4())
        effective_lease = operation.lease_seconds or lease_seconds
        job.status = "running"
        job.lease_token = token
        job.lease_owner = owner
        job.lease_until = now + timedelta(seconds=effective_lease)
        job.started_at = now
        job.error_code = None
        job.error_detail = None
        workflow.phase = operation.running_phase
        if job.operation == CLEANUP_SOURCES_AFTER_COMPLETE:
            payload = dict(workflow.payload or {})
            cleanup = dict(payload.get("source_cleanup") or {})
            cleanup.update(
                {
                    "status": "running",
                    "job_id": job.id,
                    "started_at": now.isoformat(),
                    "last_error": None,
                }
            )
            payload["source_cleanup"] = cleanup
            workflow.payload = payload
            workflow.progress = {"message": "source_cleanup_running"}
        else:
            workflow.progress = {"message": operation.running_phase}
        workflow.error_code = None
        workflow.error_detail = None
        workflow.row_version += 1
        workflow.updated_at = now
        manga.updated_at = now
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.timezone)
        self._event(
            workflow,
            "special_job_claimed",
            operation=job.operation,
            actor=owner,
            detail={"job_id": job.id, "lease_owner": owner},
        )
        self.session.flush()
        return ClaimedSpecialJob(
            job.id,
            workflow.id,
            workflow.manga_id,
            workflow.kind,
            job.operation,
            token,
            owner,
            workflow.row_version,
            manga.artifact_generation,
        )

    def validate_claim(
        self,
        job_id: int,
        *,
        workflow_id: int,
        lease_token: str,
        lease_owner: str,
    ) -> tuple[SpecialJob, SpecialWorkflow, MangaRecord] | None:
        job = self.session.get(SpecialJob, job_id)
        if (
            job is None
            or job.workflow_id != workflow_id
            or job.status != "running"
            or job.lease_token != lease_token
            or job.lease_owner != lease_owner
        ):
            return None
        workflow = self.session.get(SpecialWorkflow, workflow_id)
        manga = self.session.get(MangaRecord, workflow.manga_id) if workflow else None
        if workflow is None or manga is None:
            return None
        if job.operation == CLEANUP_SOURCES_AFTER_COMPLETE:
            if workflow.status != "completed" or manga.status != Status.COMPLETED.value:
                return None
        elif (
            workflow.status != "active"
            or manga.status != Status.SPECIAL_PROCESSING.value
        ):
            return None
        get_operation(workflow.kind, job.operation)
        return job, workflow, manga

    def renew(self, claim: ClaimedSpecialJob, *, lease_seconds: int) -> bool:
        result = self.session.execute(
            update(SpecialJob)
            .where(
                SpecialJob.id == claim.job_id,
                SpecialJob.workflow_id == claim.workflow_id,
                SpecialJob.status == "running",
                SpecialJob.lease_token == claim.lease_token,
                SpecialJob.lease_owner == claim.lease_owner,
            )
            .values(lease_until=utcnow() + timedelta(seconds=lease_seconds))
        )
        return result.rowcount == 1

    def begin_external_effect(self, claim: ClaimedSpecialJob) -> bool:
        result = self.session.execute(
            update(SpecialJob)
            .where(
                SpecialJob.id == claim.job_id,
                SpecialJob.status == "running",
                SpecialJob.lease_token == claim.lease_token,
                SpecialJob.lease_owner == claim.lease_owner,
            )
            .values(external_effect_started_at=utcnow())
        )
        return result.rowcount == 1

    def update_progress(
        self,
        claim: ClaimedSpecialJob,
        progress: dict[str, Any],
        *,
        phase: str | None = None,
    ) -> bool:
        values = self.validate_claim(
            claim.job_id,
            workflow_id=claim.workflow_id,
            lease_token=claim.lease_token,
            lease_owner=claim.lease_owner,
        )
        if values is None:
            return False
        job, workflow, manga = values
        if (workflow.payload or {}).get("cancel_requested"):
            raise SpecialCancellationRequested
        now = utcnow()
        job.progress = dict(progress)
        workflow.progress = dict(progress)
        if phase is not None:
            workflow.phase = phase
        workflow.row_version += 1
        workflow.updated_at = now
        manga.updated_at = now
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.timezone)
        return True

    def update_state(
        self,
        claim: ClaimedSpecialJob,
        *,
        payload: dict[str, Any] | None = None,
        progress: dict[str, Any] | None = None,
        phase: str | None = None,
        event_type: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        values = self.validate_claim(
            claim.job_id,
            workflow_id=claim.workflow_id,
            lease_token=claim.lease_token,
            lease_owner=claim.lease_owner,
        )
        if values is None:
            return False
        job, workflow, manga = values
        if (workflow.payload or {}).get("cancel_requested"):
            raise SpecialCancellationRequested
        now = utcnow()
        if payload is not None:
            workflow.payload = dict(payload)
        if progress is not None:
            job.progress = dict(progress)
            workflow.progress = dict(progress)
        if phase is not None:
            workflow.phase = phase
        workflow.row_version += 1
        workflow.updated_at = now
        manga.updated_at = now
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.timezone)
        if event_type:
            self._event(
                workflow,
                event_type,
                operation=job.operation,
                actor=claim.lease_owner,
                detail={"job_id": job.id, **(detail or {})},
            )
        return True

    def succeed(
        self,
        claim: ClaimedSpecialJob,
        *,
        phase: str,
        payload: dict[str, Any] | None = None,
        progress: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        values = self.validate_claim(
            claim.job_id,
            workflow_id=claim.workflow_id,
            lease_token=claim.lease_token,
            lease_owner=claim.lease_owner,
        )
        if values is None:
            return False
        job, workflow, manga = values
        if (workflow.payload or {}).get("cancel_requested"):
            raise SpecialCancellationRequested
        now = utcnow()
        job.status = "succeeded"
        job.finished_at = now
        job.lease_until = None
        job.progress = dict(progress or job.progress or {})
        workflow.phase = phase
        if payload is not None:
            workflow.payload = dict(payload)
        workflow.progress = dict(progress or workflow.progress or {})
        workflow.error_code = None
        workflow.error_detail = None
        workflow.row_version += 1
        workflow.updated_at = now
        manga.last_error_operation = None
        manga.last_error_code = None
        manga.last_error_detail = None
        manga.last_error_at = None
        manga.updated_at = now
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.timezone)
        self._event(
            workflow,
            "special_job_succeeded",
            operation=job.operation,
            actor=claim.lease_owner,
            detail={"job_id": job.id, "phase": phase, **(detail or {})},
        )
        return True

    def fail(
        self,
        claim: ClaimedSpecialJob,
        *,
        error_code: str,
        error_detail: str,
        phase: str = "failed",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        values = self.validate_claim(
            claim.job_id,
            workflow_id=claim.workflow_id,
            lease_token=claim.lease_token,
            lease_owner=claim.lease_owner,
        )
        if values is None:
            return False
        job, workflow, manga = values
        now = utcnow()
        if job.operation == CLEANUP_SOURCES_AFTER_COMPLETE:
            job.status = "failed"
            job.finished_at = now
            job.lease_until = None
            job.error_code = error_code
            job.error_detail = error_detail[:4000]
            payload_value = dict(payload if payload is not None else workflow.payload or {})
            cleanup = dict(payload_value.get("source_cleanup") or {})
            cleanup.update(
                {
                    "status": "failed",
                    "job_id": job.id,
                    "last_error": error_detail[:4000],
                    "last_error_code": error_code,
                    "finished_at": now.isoformat(),
                }
            )
            payload_value["source_cleanup"] = cleanup
            workflow.payload = payload_value
            workflow.phase = "ready"
            workflow.progress = {"message": "source_cleanup_failed"}
            workflow.error_code = error_code
            workflow.error_detail = error_detail[:4000]
            workflow.row_version += 1
            workflow.updated_at = now
            manga.updated_at = now
            manga.row_version += 1
            sync_remark(manga, workflow, timezone=self.timezone)
            self._event(
                workflow,
                "special_source_cleanup_failed",
                operation=job.operation,
                actor=claim.lease_owner,
                error_code=error_code,
                detail={"job_id": job.id, "summary": error_detail[:500]},
            )
            return True
        job.status = "failed"
        job.finished_at = now
        job.lease_until = None
        job.error_code = error_code
        job.error_detail = error_detail[:4000]
        workflow.phase = phase
        workflow.error_code = error_code
        workflow.error_detail = error_detail[:4000]
        current_payload = dict(payload if payload is not None else workflow.payload or {})
        current_payload["retry_operation"] = job.operation
        workflow.payload = current_payload
        workflow.progress = {"message": "failed"}
        workflow.row_version += 1
        workflow.updated_at = now
        manga.last_error_operation = "special_processing"
        manga.last_error_code = error_code
        manga.last_error_detail = error_detail[:4000]
        manga.last_error_at = now
        manga.updated_at = now
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.timezone)
        self._event(
            workflow,
            "special_job_failed",
            operation=job.operation,
            actor=claim.lease_owner,
            error_code=error_code,
            detail={"job_id": job.id, "summary": error_detail[:500]},
        )
        return True

    def cancel_complete(self, claim: ClaimedSpecialJob, *, detail: dict[str, Any]) -> bool:
        values = self.validate_claim(
            claim.job_id,
            workflow_id=claim.workflow_id,
            lease_token=claim.lease_token,
            lease_owner=claim.lease_owner,
        )
        if values is None:
            return False
        job, workflow, manga = values
        now = utcnow()
        previous = manga.status
        job.status = "succeeded" if job.operation == "cancel_video_archive" else "cancelled"
        job.finished_at = now
        job.lease_until = None
        workflow.phase = "cancelled"
        workflow.status = "cancelled"
        workflow.completed_at = now
        workflow.error_code = None
        workflow.error_detail = None
        workflow.row_version += 1
        workflow.updated_at = now
        manga.status = workflow.resume_status or Status.MANUAL_REVIEW.value
        manga.status_updated_at = manga.updated_at = now
        restore_entry_error(manga, workflow, restored_at=now)
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.timezone)
        self._event(
            workflow,
            "special_cancelled",
            operation=job.operation,
            actor=claim.lease_owner,
            from_status=previous,
            to_status=manga.status,
            detail={"job_id": job.id, **detail},
        )
        return True

    def complete_video_archive(
        self,
        claim: ClaimedSpecialJob,
        *,
        payload: dict[str, Any],
        fingerprint: ArtifactFingerprint,
        generation: int,
        detail: dict[str, Any],
    ) -> bool:
        values = self.validate_claim(
            claim.job_id,
            workflow_id=claim.workflow_id,
            lease_token=claim.lease_token,
            lease_owner=claim.lease_owner,
        )
        if values is None:
            return False
        job, workflow, manga = values
        if (workflow.payload or {}).get("cancel_requested"):
            raise SpecialCancellationRequested
        if manga.artifact_generation != claim.artifact_generation:
            return False
        now = utcnow()
        previous = manga.status
        final_payload = dict(payload)
        final_payload["final_artifact"] = {
            "location": "prepared",
            "filename": fingerprint.path.name,
            "kind": fingerprint.kind,
            "generation": generation,
            "size": fingerprint.size,
            "sha1": fingerprint.sha1,
            "checked_at": fingerprint.checked_at.isoformat(),
        }
        job.status = "succeeded"
        job.finished_at = now
        job.lease_until = None
        job.progress = {"message": "ready", "completed": 1, "total": 1}
        workflow.status = "completed"
        workflow.phase = "ready"
        workflow.payload = final_payload
        workflow.progress = {"message": "ready", "completed": 1, "total": 1}
        workflow.error_code = None
        workflow.error_detail = None
        workflow.completed_at = now
        workflow.updated_at = now
        workflow.row_version += 1
        manga.status = Status.DOWNLOADED.value
        manga.status_updated_at = manga.updated_at = now
        manga.download_method = "torrent"
        manga.external_download_id = None
        manga.artifact_location = "prepared"
        manga.artifact_filename = fingerprint.path.name
        manga.artifact_kind = fingerprint.kind
        manga.artifact_generation = generation
        manga.artifact_size = fingerprint.size
        manga.artifact_sha1 = fingerprint.sha1
        manga.artifact_checked_at = fingerprint.checked_at
        manga.last_error_operation = None
        manga.last_error_code = None
        manga.last_error_detail = None
        manga.last_error_at = None
        manga.next_retry_at = None
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.timezone)
        self._event(
            workflow,
            "special_completed",
            operation=job.operation,
            actor=claim.lease_owner,
            from_status=previous,
            to_status=manga.status,
            detail={
                "job_id": job.id,
                "artifact_filename": fingerprint.path.name,
                "artifact_generation": generation,
                "artifact_size": fingerprint.size,
                "artifact_sha1": fingerprint.sha1,
                **detail,
            },
        )
        return True

    def complete_source_cleanup(
        self,
        claim: ClaimedSpecialJob,
        *,
        detail: dict[str, Any],
    ) -> bool:
        values = self.validate_claim(
            claim.job_id,
            workflow_id=claim.workflow_id,
            lease_token=claim.lease_token,
            lease_owner=claim.lease_owner,
        )
        if values is None:
            return False
        job, workflow, manga = values
        now = utcnow()
        payload = dict(workflow.payload or {})
        cleanup = dict(payload.get("source_cleanup") or {})
        cleanup.update(
            {
                "status": "completed",
                "job_id": job.id,
                "last_error": None,
                "last_error_code": None,
                "finished_at": now.isoformat(),
                "detail": dict(detail),
            }
        )
        payload["source_cleanup"] = cleanup
        job.status = "succeeded"
        job.finished_at = now
        job.lease_until = None
        job.progress = {"message": "source_cleanup_completed", "completed": 1, "total": 1}
        workflow.payload = payload
        workflow.phase = "ready"
        workflow.progress = {"message": "source_cleanup_completed", "completed": 1, "total": 1}
        workflow.error_code = None
        workflow.error_detail = None
        workflow.updated_at = now
        workflow.row_version += 1
        manga.updated_at = now
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.timezone)
        self._event(
            workflow,
            "special_source_cleanup_completed",
            operation=job.operation,
            actor=claim.lease_owner,
            detail={"job_id": job.id, **detail},
        )
        return True

    @staticmethod
    def _claimable_state():
        return or_(
            and_(
                SpecialJob.operation != CLEANUP_SOURCES_AFTER_COMPLETE,
                SpecialWorkflow.status == "active",
                MangaRecord.status == Status.SPECIAL_PROCESSING.value,
            ),
            and_(
                SpecialJob.operation == CLEANUP_SOURCES_AFTER_COMPLETE,
                SpecialWorkflow.status == "completed",
                SpecialWorkflow.phase == "ready",
                MangaRecord.status == Status.COMPLETED.value,
            ),
        )

    def _event(
        self,
        workflow: SpecialWorkflow,
        event_type: str,
        *,
        operation: str | None,
        actor: str,
        from_status: str | None = None,
        to_status: str | None = None,
        error_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            EventLog(
                manga_id=workflow.manga_id,
                run_id=self.run_id,
                component="special_processing",
                event_type=event_type,
                operation=operation,
                from_status=from_status,
                to_status=to_status,
                error_code=error_code,
                actor=actor,
                detail={"workflow_id": workflow.id, **(detail or {})},
            )
        )
