from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    DISCOVERED = "discovered"
    DEFERRED = "deferred"
    DOWNLOAD_PENDING = "download_pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    VALIDATING = "validating"
    PREPARING = "preparing"
    UPLOAD_PENDING = "upload_pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    COMPLETED = "completed"
    QUARANTINED = "quarantined"
    MANUAL_REVIEW = "manual_review"
    SPECIAL_PROCESSING = "special_processing"
    FILTERED_OUT = "filtered_out"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    OUTDATED = "outdated"
    FORCE_DELETE_PENDING = "force_delete_pending"
    RENAME_PENDING = "rename_pending"
    DELETED = "deleted"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class QueueSource(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class DownloadMethod(StrEnum):
    TORRENT = "torrent"
    DIRECT = "direct"
    HAH = "hah"
    ARIA2 = "aria2"


REPLACEMENT_DELETE_READY_STATUSES = frozenset(
    {
        Status.DOWNLOAD_PENDING.value,
        Status.DOWNLOADING.value,
        Status.DOWNLOADED.value,
        Status.RENAME_PENDING.value,
        Status.VALIDATING.value,
        Status.PREPARING.value,
        Status.UPLOAD_PENDING.value,
        Status.UPLOADING.value,
        Status.UPLOADED.value,
        Status.COMPLETED.value,
    }
)


# Event names are deliberately stable. The transition service is the only
# runtime entry point allowed to change a Manga.status value.
TRANSITIONS: dict[str, dict[str, str]] = {
    Status.DISCOVERED: {
        "defer": Status.DEFERRED,
        "queue": Status.DOWNLOAD_PENDING,
        "skip": Status.SKIPPED,
        "unavailable": Status.UNAVAILABLE,
        "review": Status.MANUAL_REVIEW,
    },
    Status.DEFERRED: {"resume": Status.DISCOVERED, "cancel": Status.CANCEL_REQUESTED},
    Status.DOWNLOAD_PENDING: {
        "download_started": Status.DOWNLOADING,
        "details_retry": Status.DOWNLOAD_PENDING,
        "unavailable": Status.UNAVAILABLE,
        "cancel": Status.CANCEL_REQUESTED,
    },
    Status.DOWNLOADING: {
        "downloaded": Status.DOWNLOADED,
        "fallback": Status.DOWNLOAD_PENDING,
        "retry": Status.DOWNLOAD_PENDING,
        "details_retry": Status.DOWNLOADING,
        "unavailable": Status.UNAVAILABLE,
        "cancel": Status.CANCEL_REQUESTED,
    },
    Status.DOWNLOADED: {
        "validate": Status.VALIDATING,
        "details_retry": Status.DOWNLOADED,
        "cancel": Status.CANCEL_REQUESTED,
    },
    Status.VALIDATING: {
        "upload": Status.UPLOAD_PENDING,
        "prepare": Status.PREPARING,
        "quarantine": Status.QUARANTINED,
        "review": Status.MANUAL_REVIEW,
        "retry": Status.DOWNLOADED,
        "rename_retry": Status.RENAME_PENDING,
        "details_retry": Status.VALIDATING,
    },
    Status.PREPARING: {
        "ready": Status.UPLOAD_PENDING,
        "retry": Status.DOWNLOADED,
        "review": Status.MANUAL_REVIEW,
    },
    Status.UPLOAD_PENDING: {
        "upload_started": Status.UPLOADING,
        "details_retry": Status.UPLOAD_PENDING,
        "revalidate": Status.VALIDATING,
        "cancel": Status.CANCEL_REQUESTED,
    },
    Status.UPLOADING: {
        "uploaded": Status.UPLOADED,
        "retry": Status.UPLOAD_PENDING,
        "review": Status.MANUAL_REVIEW,
        "revalidate": Status.VALIDATING,
        "quarantine": Status.QUARANTINED,
    },
    Status.UPLOADED: {
        "cleanup": Status.COMPLETED,
        "cleanup_retry": Status.UPLOADED,
        "outdate": Status.OUTDATED,
    },
    Status.COMPLETED: {"outdate": Status.OUTDATED},
    Status.OUTDATED: {"deleted": Status.DELETED, "review": Status.MANUAL_REVIEW},
    Status.FORCE_DELETE_PENDING: {
        "deleted": Status.DELETED,
        "review": Status.MANUAL_REVIEW,
    },
    Status.RENAME_PENDING: {"validate": Status.VALIDATING},
    Status.SKIPPED: {"override": Status.DOWNLOAD_PENDING, "cancel": Status.CANCEL_REQUESTED},
    Status.UNAVAILABLE: {"retry": Status.DOWNLOAD_PENDING, "cancel": Status.CANCEL_REQUESTED},
    Status.QUARANTINED: {
        "redownload": Status.DOWNLOAD_PENDING,
        "replace": Status.VALIDATING,
        "cancel": Status.CANCEL_REQUESTED,
    },
    Status.MANUAL_REVIEW: {
        "special_start": Status.SPECIAL_PROCESSING,
        "rename": Status.RENAME_PENDING,
        "resume_download": Status.DOWNLOAD_PENDING,
        "resume_validate": Status.VALIDATING,
        "resume_upload": Status.UPLOAD_PENDING,
        "resume_outdated": Status.OUTDATED,
        "confirm_uploaded": Status.UPLOADED,
        "cancel": Status.CANCEL_REQUESTED,
    },
    Status.FILTERED_OUT: {
        "rescreen": Status.DISCOVERED,
        "override": Status.DOWNLOAD_PENDING,
        "cancel": Status.CANCEL_REQUESTED,
    },
    Status.SPECIAL_PROCESSING: {
        "special_downloaded": Status.DOWNLOADED,
        "special_cancel": Status.MANUAL_REVIEW,
    },
    Status.CANCEL_REQUESTED: {"cancelled": Status.CANCELLED},
    Status.CANCELLED: {
        "resume": Status.DOWNLOAD_PENDING,
        "resume_validate": Status.VALIDATING,
        "resume_upload": Status.UPLOAD_PENDING,
        "resume_uploaded": Status.UPLOADED,
    },
    Status.DELETED: {},
}


def transition_target(current: Status | str, event: str) -> Status:
    current = Status(current)
    try:
        return Status(TRANSITIONS[current][event])
    except KeyError as exc:
        raise ValueError(f"Transition {current.value!r} + {event!r} is not allowed") from exc


def can_transition(current: Status | str, event: str) -> bool:
    try:
        transition_target(current, event)
    except (ValueError, KeyError):
        return False
    return True
