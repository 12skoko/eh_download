from ....db.models import MangaRecord
from ....db.repository import utcnow
from ...integrations.manga import MangaIntegration, binding
from .definition import CLEANUP_SOURCES_AFTER_COMPLETE
from .remarks import restore_entry_error, sync_remark


class VideoIntegration(MangaIntegration):
    def validate(self, session, workflow, job):
        b = binding(workflow)
        rows = self.records(session, workflow)
        if len(rows) != 1:
            return False
        manga = rows[0]
        expected = (
            "completed" if job.operation == CLEANUP_SOURCES_AFTER_COMPLETE else "special_processing"
        )
        if (
            manga.status != expected
            or manga.active_attempt_id
            or manga.lease_token
            or manga.lease_owner
        ):
            return False
        context = dict(b.context or {})
        if job.status == "running":
            generation = context.get("expected_artifact_generation")
            version = context.get("expected_manga_version")
            if generation != manga.artifact_generation or (
                version is not None and version != manga.row_version
            ):
                return False
        return True

    def changed(self, repository, workflow, job, event):
        manga = repository.session.get(MangaRecord, binding(workflow).manga_id)
        now = utcnow()
        cleanup = job.operation == CLEANUP_SOURCES_AFTER_COMPLETE
        if cleanup and event in {"claimed", "failed"}:
            payload = dict(workflow.payload or {})
            state = dict(payload.get("source_cleanup") or {})
            state.update(
                status="running" if event == "claimed" else "failed",
                job_id=job.id,
                last_error=workflow.error_detail,
                last_error_code=workflow.error_code,
            )
            state["started_at" if event == "claimed" else "finished_at"] = now.isoformat()
            payload["source_cleanup"] = state
            workflow.payload = payload
            workflow.progress = {"message": f"source_cleanup_{state['status']}"}
        if event == "failed" and not cleanup:
            workflow.payload = {**(workflow.payload or {}), "retry_operation": job.operation}
            manga.last_error_operation = "special_processing"
            manga.last_error_code, manga.last_error_detail = (
                workflow.error_code,
                workflow.error_detail,
            )
            manga.last_error_at = now
        if event == "succeeded":
            manga.last_error_operation = manga.last_error_code = manga.last_error_detail = (
                manga.last_error_at
            ) = None
        if event == "cancelled":
            manga.status = binding(workflow).resume_status or "manual_review"
            manga.status_updated_at = now
            restore_entry_error(manga, workflow, restored_at=now)
        manga.row_version += 1
        manga.updated_at = now
        b = binding(workflow)
        b.context = {
            **b.context,
            "expected_manga_version": manga.row_version,
            "expected_artifact_generation": manga.artifact_generation,
        }
        sync_remark(manga, workflow, timezone=repository.timezone)
