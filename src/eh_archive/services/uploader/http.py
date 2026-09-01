from __future__ import annotations

from .contracts import UploadOutcome, UploadRequest
from .lanraragi import (
    AUTH_HTTP_STATUSES,
    RETRYABLE_HTTP_STATUSES,
    LANraragiApiGateway,
    lanraragi_archive_id,
)


class HttpUploadBackend:
    """LANraragi's streaming multipart upload backend."""

    def __init__(self, api: LANraragiApiGateway) -> None:
        self.api = api

    def upload(self, request: UploadRequest) -> UploadOutcome:
        expected_id = request.expected_archive_id or lanraragi_archive_id(request.path)
        request.archive_identified(expected_id)
        request.phase("checking_lanraragi", {"expected_archive_id": expected_id})
        status, existing = self.api.metadata(expected_id)
        if status == 200:
            if not self.api.metadata_matches_artifact(
                existing,
                archive_id=expected_id,
                size=request.size,
                filename=request.filename,
            ):
                return UploadOutcome(
                    "duplicate_review",
                    expected_id,
                    status,
                    "the expected LANraragi ID exists with a different size or filename",
                    "lrr_prefix_collision",
                )
            return self._update_and_confirm(request, expected_id, upload_status=status)
        if status in AUTH_HTTP_STATUSES:
            return UploadOutcome(
                "system",
                expected_id,
                status,
                "LANraragi rejected metadata authentication",
                "lanraragi_authentication_failed",
            )
        if status not in {400, 404}:
            kind = "retry" if status in RETRYABLE_HTTP_STATUSES else "review"
            return UploadOutcome(
                kind,
                expected_id,
                status,
                "unexpected metadata preflight response",
                "lrr_metadata_preflight_failed",
            )

        request.phase("uploading", {"expected_archive_id": expected_id})
        outcome = self.api.upload_archive(
            request.path,
            request.info,
            checksum=request.sha1,
            timeout=request.timeout,
        )
        if outcome.kind != "success" or not outcome.archive_id:
            return outcome

        archive_id = outcome.archive_id
        if archive_id != expected_id:
            return UploadOutcome(
                "unknown",
                archive_id,
                outcome.status_code,
                "LANraragi returned an archive ID different from the expected prefix SHA-1",
                "lrr_archive_id_mismatch",
            )
        request.archive_identified(archive_id)
        return self._update_and_confirm(
            request,
            archive_id,
            upload_status=outcome.status_code,
            upload_response=outcome.response,
        )

    def _update_and_confirm(
        self,
        request: UploadRequest,
        archive_id: str,
        *,
        upload_status: int | None,
        upload_response: str = "",
    ) -> UploadOutcome:
        request.phase("metadata_updating", {"expected_archive_id": archive_id})
        expected_metadata = self.api.metadata_values(request.info)
        metadata_outcome = self.api.update_metadata(
            archive_id,
            request.info,
            metadata=expected_metadata,
        )
        if metadata_outcome.kind != "success":
            return UploadOutcome(
                metadata_outcome.kind,
                archive_id,
                metadata_outcome.status_code,
                metadata_outcome.response,
                metadata_outcome.error_code,
            )

        request.phase("confirming", {"expected_archive_id": archive_id})
        confirmed = self.api.confirm_metadata(
            archive_id,
            size=request.size,
            filename=request.filename,
            expected=expected_metadata,
        )
        if confirmed.kind != "success":
            return UploadOutcome(
                confirmed.kind,
                archive_id,
                confirmed.status_code,
                confirmed.response,
                confirmed.error_code,
            )
        request.phase("confirmed", {"expected_archive_id": archive_id})
        return UploadOutcome("success", archive_id, upload_status, upload_response)
