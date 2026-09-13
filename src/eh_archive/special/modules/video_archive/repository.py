from typing import Any

from ....db.models import MangaRecord
from ....db.repository import utcnow
from ....domain.states import Status
from ....services.validator.artifact import ArtifactFingerprint
from ...core.repository import ClaimedSpecialJob, SpecialCancellationRequested
from ...core.repository import SpecialRepository as CoreRepository
from ...integrations.manga import binding
from .remarks import sync_remark


class SpecialRepository(CoreRepository):
    def active_for_manga(self, manga_id):
        from ...integrations.manga import active_for_manga

        return active_for_manga(self.session, manga_id)

    def validate_video_claim(self, *args, **kwargs):
        values = self.validate_claim(*args, **kwargs)
        if values is None:
            return None
        job, workflow = values
        manga = self.session.get(MangaRecord, binding(workflow).manga_id)
        return job, workflow, manga

    def complete_video_archive(
        self,
        claim: ClaimedSpecialJob,
        *,
        payload: dict[str, Any],
        fingerprint: ArtifactFingerprint,
        generation: int,
        detail: dict[str, Any],
    ) -> bool:
        values = self.validate_video_claim(
            claim.job_id,
            workflow_id=claim.workflow_id,
            lease_token=claim.lease_token,
            lease_owner=claim.lease_owner,
        )
        if values is None:
            return False
        job, workflow, manga = values
        if workflow.cancel_requested_at:
            raise SpecialCancellationRequested
        if manga.artifact_generation != binding(workflow).context.get(
            "expected_artifact_generation"
        ):
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
        self.release_resources(workflow, job=job)
        return True

    def complete_source_cleanup(
        self,
        claim: ClaimedSpecialJob,
        *,
        detail: dict[str, Any],
    ) -> bool:
        values = self.validate_video_claim(
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
        self.release_resources(workflow, job=job)
        return True
