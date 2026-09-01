from __future__ import annotations

import hashlib
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, Protocol, Self

from ...domain.errors import ArchiveError
from .contracts import UploadOutcome, UploadRequest
from .lanraragi import (
    AUTH_HTTP_STATUSES,
    RETRYABLE_HTTP_STATUSES,
    LANraragiApiGateway,
    lanraragi_archive_id,
)

DEFAULT_TRANSFER_CHUNK_BYTES = 4 * 1024 * 1024


class RemoteStore(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type, exc, traceback) -> None: ...

    def exists(self, filename: str) -> bool: ...

    def size(self, filename: str) -> int: ...

    def open_write(self, filename: str) -> BinaryIO: ...

    def open_read(self, filename: str) -> BinaryIO: ...

    def remove(self, filename: str) -> None: ...

    def rename(self, source: str, destination: str) -> None: ...


def _sha1_stream(stream: BinaryIO, *, chunk_size: int, checkpoint) -> str:
    digest = hashlib.sha1()
    while chunk := stream.read(chunk_size):
        digest.update(chunk)
        checkpoint()
    return digest.hexdigest()


def _sha1_file(path: Path, *, chunk_size: int, checkpoint) -> str:
    with path.open("rb") as stream:
        return _sha1_stream(stream, chunk_size=chunk_size, checkpoint=checkpoint)


class FilesystemUploadBackend:
    """Publish a verified archive into LANraragi's watched directory through SMB."""

    def __init__(
        self,
        api: LANraragiApiGateway,
        store: RemoteStore,
        *,
        poll_timeout: float = 600.0,
        poll_interval: float = 3.0,
        chunk_size: int = DEFAULT_TRANSFER_CHUNK_BYTES,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        if poll_timeout <= 0:
            raise ValueError("lanraragi_import_poll_timeout_seconds must be positive")
        if poll_interval <= 0:
            raise ValueError("lanraragi_import_poll_interval_seconds must be positive")
        if chunk_size <= 0:
            raise ValueError("SMB transfer chunk size must be positive")
        self.api = api
        self.store = store
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self.chunk_size = chunk_size
        self.sleep = sleep
        self.monotonic = monotonic

    def upload(self, request: UploadRequest) -> UploadOutcome:
        invalid = self._validate_request(request)
        if invalid is not None:
            return invalid

        request.phase("validating_local", {})
        request.checkpoint()
        full_sha1 = _sha1_file(
            request.path,
            chunk_size=self.chunk_size,
            checkpoint=request.checkpoint,
        )
        if full_sha1 != request.sha1.lower():
            return UploadOutcome(
                "revalidate",
                response="local artifact SHA-1 no longer matches the database fingerprint",
                error_code="artifact_sha1_changed",
            )
        expected_id = request.expected_archive_id or lanraragi_archive_id(request.path)
        request.archive_identified(expected_id)
        self.api.shinobu_status()

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
            return self._update_and_confirm(request, expected_id)
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

        staging = f".{request.filename}.{request.attempt_id}.uploading"
        published = False
        staging_owned = False
        request.phase(
            "preparing_remote",
            {
                "expected_archive_id": expected_id,
                "remote_filename": request.filename,
                "staging_filename": staging,
            },
        )
        try:
            with self.store:
                try:
                    if self.store.exists(request.filename):
                        request.phase("verifying_remote", {"remote_filename": request.filename})
                        if not self._remote_matches(request, request.filename):
                            return UploadOutcome(
                                "review",
                                expected_id,
                                response="remote final filename already exists with different bytes",
                                error_code="smb_final_conflict",
                            )
                        published = True
                    else:
                        if self.store.exists(staging):
                            staging_owned = True
                            request.phase("verifying_remote", {"staging_filename": staging})
                            if not self._remote_matches(request, staging):
                                self.store.remove(staging)
                                staging_owned = False
                        if not staging_owned:
                            # This name belongs exclusively to the current
                            # attempt, including partial files left by a failed write.
                            staging_owned = True
                            self._transfer(request, staging)
                        request.phase("verifying_remote", {"staging_filename": staging})
                        if not self._remote_matches(request, staging):
                            return UploadOutcome(
                                "review",
                                expected_id,
                                response="remote staging size or SHA-1 did not match the source",
                                error_code="smb_integrity_mismatch",
                            )
                        request.checkpoint()
                        request.phase(
                            "publishing",
                            {
                                "remote_filename": request.filename,
                                "staging_filename": staging,
                            },
                        )
                        self.store.rename(staging, request.filename)
                        staging_owned = False
                        published = True
                    request.phase(
                        "published",
                        {
                            "expected_archive_id": expected_id,
                            "remote_filename": request.filename,
                        },
                    )
                finally:
                    if staging_owned:
                        with suppress(Exception):
                            if self.store.exists(staging):
                                self.store.remove(staging)
        except ArchiveError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify every SMB library error locally
            return self._store_failure(exc, expected_id=expected_id, published=published)

        return self._wait_and_finalize(request, expected_id)

    @staticmethod
    def _validate_request(request: UploadRequest) -> UploadOutcome | None:
        if (
            not request.path.is_file()
            or request.path.name != request.filename
            or request.path.stat().st_size != request.size
        ):
            return UploadOutcome(
                "revalidate",
                response="upload source path, basename, or size changed",
                error_code="artifact_changed",
            )
        if (
            not request.filename
            or request.filename in {".", ".."}
            or any(char in request.filename for char in "\\/")
        ):
            return UploadOutcome(
                "review",
                response="artifact filename is not an unchanged basename",
                error_code="artifact_filename_invalid",
            )
        if not re.fullmatch(r"[0-9a-fA-F]{40}", request.sha1):
            return UploadOutcome(
                "revalidate",
                response="artifact SHA-1 is invalid",
                error_code="artifact_sha1_invalid",
            )
        return None

    def _transfer(self, request: UploadRequest, staging: str) -> None:
        request.phase(
            "transferring",
            {
                "remote_filename": request.filename,
                "staging_filename": staging,
            },
        )
        transferred = 0
        with request.path.open("rb") as local, self.store.open_write(staging) as remote:
            while chunk := local.read(self.chunk_size):
                request.checkpoint()
                remote.write(chunk)
                transferred += len(chunk)
                request.progress(transferred, request.size)
            remote.flush()
        if transferred != request.size:
            raise OSError("source size changed during SMB transfer")

    def _remote_matches(self, request: UploadRequest, filename: str) -> bool:
        if self.store.size(filename) != request.size:
            return False
        with self.store.open_read(filename) as remote:
            remote_sha1 = _sha1_stream(
                remote,
                chunk_size=self.chunk_size,
                checkpoint=request.checkpoint,
            )
        return remote_sha1 == request.sha1.lower()

    def _wait_and_finalize(self, request: UploadRequest, archive_id: str) -> UploadOutcome:
        request.phase("waiting_for_shinobu", {"expected_archive_id": archive_id})
        deadline = self.monotonic() + self.poll_timeout
        last_status: int | None = None
        while self.monotonic() < deadline:
            request.checkpoint()
            try:
                status, payload = self.api.metadata(archive_id)
            except Exception as exc:  # noqa: BLE001 - published result is intentionally unknown
                return UploadOutcome(
                    "unknown",
                    archive_id,
                    response=f"published archive could not be confirmed ({type(exc).__name__})",
                    error_code="lrr_published_confirmation_unknown",
                )
            last_status = status
            if status == 200:
                if not self.api.metadata_matches_artifact(
                    payload,
                    archive_id=archive_id,
                    size=request.size,
                    filename=request.filename,
                ):
                    return UploadOutcome(
                        "review",
                        archive_id,
                        status,
                        "Shinobu imported archive metadata with a different size or filename",
                        "lrr_import_mismatch",
                    )
                return self._update_and_confirm(request, archive_id)
            if status in AUTH_HTTP_STATUSES:
                return UploadOutcome(
                    "system",
                    archive_id,
                    status,
                    "LANraragi rejected metadata authentication",
                    "lanraragi_authentication_failed",
                )
            if status not in {400, 404, *RETRYABLE_HTTP_STATUSES}:
                return UploadOutcome(
                    "review",
                    archive_id,
                    status,
                    "unexpected response while waiting for Shinobu",
                    "lrr_import_failed",
                )
            self.sleep(self.poll_interval)
        return UploadOutcome(
            "unknown",
            archive_id,
            last_status,
            "published file was not imported before the Shinobu polling timeout",
            "lrr_import_timeout",
        )

    def _update_and_confirm(self, request: UploadRequest, archive_id: str) -> UploadOutcome:
        request.phase("metadata_updating", {"expected_archive_id": archive_id})
        expected_metadata = self.api.metadata_values(request.info)
        try:
            updated = self.api.update_metadata(
                archive_id,
                request.info,
                metadata=expected_metadata,
            )
        except Exception as exc:  # noqa: BLE001 - published result is intentionally unknown
            return UploadOutcome(
                "unknown",
                archive_id,
                response=f"metadata update result is unknown ({type(exc).__name__})",
                error_code="lrr_metadata_update_unknown",
            )
        if updated.kind != "success":
            return UploadOutcome(
                updated.kind,
                archive_id,
                updated.status_code,
                updated.response,
                updated.error_code,
            )
        request.phase("confirming", {"expected_archive_id": archive_id})
        try:
            confirmed = self.api.confirm_metadata(
                archive_id,
                size=request.size,
                filename=request.filename,
                expected=expected_metadata,
            )
        except Exception as exc:  # noqa: BLE001 - published result is intentionally unknown
            return UploadOutcome(
                "unknown",
                archive_id,
                response=f"metadata confirmation result is unknown ({type(exc).__name__})",
                error_code="lrr_metadata_confirmation_unknown",
            )
        if confirmed.kind == "success":
            request.phase("confirmed", {"expected_archive_id": archive_id})
        return confirmed

    @staticmethod
    def _store_failure(
        exc: Exception, *, expected_id: str, published: bool
    ) -> UploadOutcome:
        name = type(exc).__name__
        folded = f"{name} {exc}".casefold()
        if isinstance(exc, FileExistsError):
            return UploadOutcome(
                "review",
                expected_id,
                response="SMB final filename already exists",
                error_code="smb_final_conflict",
            )
        if isinstance(exc, (ValueError, NotADirectoryError, ImportError, RuntimeError)):
            return UploadOutcome(
                "system",
                expected_id,
                response=f"SMB configuration is unusable ({name})",
                error_code="smb_configuration_error",
            )
        if any(
            marker in folded
            for marker in ("object_name_invalid", "name too long", "invalid filename")
        ):
            return UploadOutcome(
                "review",
                expected_id,
                response=f"source basename is not accepted by the SMB target ({name})",
                error_code="smb_filename_invalid",
            )
        if isinstance(exc, PermissionError) or any(
            marker in folded for marker in ("accessdenied", "permission", "logon", "auth")
        ):
            return UploadOutcome(
                "system",
                expected_id,
                response=f"SMB authentication or permission failed ({name})",
                error_code="smb_authentication_or_permission_failed",
            )
        return UploadOutcome(
            "unknown" if published else "retry",
            expected_id,
            response=f"SMB operation failed ({name})",
            error_code="smb_published_unknown" if published else "smb_transfer_failed",
        )
