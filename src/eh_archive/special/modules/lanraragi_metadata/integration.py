"""Manga lifecycle coordination; no remote I/O and no scheduler dependency."""

from sqlalchemy import select

from ....db.models import JobAttempt
from ....db.repository import ArchiveRepository, utcnow
from ....services.lanraragi_metadata import fingerprint, manga_info
from ....services.uploader.lanraragi import build_tags
from ...integrations.manga import MangaIntegration


def idle(row):
    return not any((row.active_attempt_id, row.lease_token, row.lease_owner, row.lease_until))


def eligible(row):
    return (
        idle(row)
        and not row.superseded_by_id
        and (
            row.status == "completed"
            or (
                row.status == "manual_review"
                and row.last_error_code == "lrr_metadata_mismatch"
                and row.last_error_operation == "upload"
            )
        )
    )


def snapshot(session, row):
    info = manga_info(row.info)
    attempt = session.scalar(
        select(JobAttempt)
        .where(
            JobAttempt.manga_id == row.manga_id,
            JobAttempt.operation == "upload",
            JobAttempt.artifact_generation == row.artifact_generation,
        )
        .order_by(JobAttempt.id.desc())
        .limit(1)
    )
    archive_id = row.lrr_archive_id
    if not archive_id and attempt:
        archive_id = (attempt.detail or {}).get("expected_archive_id")
    return {
        "manga_id": row.manga_id,
        "archive_id": archive_id,
        "filename": row.artifact_filename,
        "size": row.artifact_size,
        "generation": row.artifact_generation,
        "version": row.row_version,
        "status": row.status,
        "info_hash": fingerprint({"title": info.name, "tags": build_tags(info, date_added=1)}),
    }


def unchanged(session, row, original, *, reserved=False):
    if not idle(row) or row.superseded_by_id:
        return False
    current = snapshot(session, row)
    if reserved:
        current["status"] = original["status"]
    return current == original


def restore(row, binding):
    context = binding.context or {}
    if context.get("reserved") and row.status == "special_processing" and idle(row):
        row.status = binding.resume_status
        row.status_updated_at = row.updated_at = utcnow()
        row.row_version += 1
        binding.context = {**context, "reserved": False}


class MetadataIntegration(MangaIntegration):
    # Checks are per item: one stale row must not prevent reporting other rows.
    def changed(self, repository, workflow, job, event):
        if event not in {"failed", "cancelled", "succeeded"}:
            return
        if job.operation != "apply":
            return
        rows = {r.manga_id: r for r in self.records(repository.session, workflow)}
        for b in workflow.manga_bindings:
            restore(rows[b.manga_id], b)


def reserve(session, row, binding):
    if not eligible(row) or not unchanged(session, row, binding.context["snapshot"]):
        raise ValueError(f"档案 {row.manga_id} 在预览后已变化，请重新预览")
    row.status = "special_processing"
    row.row_version += 1
    row.status_updated_at = row.updated_at = utcnow()
    expected = {**binding.context["snapshot"], "version": row.row_version}
    binding.context = {**binding.context, "snapshot": expected, "reserved": True}


def record_success(repository, workflow, binding, row, archive_id, result, actor):
    original = binding.context["snapshot"]
    if row.status != "special_processing" or not unchanged(
        repository.session, row, original, reserved=True
    ):
        raise ValueError("档案在更新期间变化，远端结果需要重新复核")
    restore(row, binding)
    ArchiveRepository(repository.session).confirm_remote_metadata(
        row, archive_id=archive_id, actor=actor, detail={"workflow_id": workflow.id}
    )
    binding.context = {**binding.context, "result": result, "archive_id": archive_id}
