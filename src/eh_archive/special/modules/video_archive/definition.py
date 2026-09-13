from __future__ import annotations

from dataclasses import dataclass

from ...core.contracts import OperationDefinition, WorkflowDefinition

VIDEO_ARCHIVE_KIND = "video_archive"

LOAD_TORRENT_OPTIONS = "load_torrent_options"
SUBMIT_SELECTED_TORRENTS = "submit_selected_torrents"
CHECK_AND_COMPOSE = "check_and_compose_if_ready"
CANCEL_VIDEO_ARCHIVE = "cancel_video_archive"
CLEANUP_SOURCES_AFTER_COMPLETE = "cleanup_sources_after_complete"


@dataclass(frozen=True)
class VideoWorkflowDefinition(WorkflowDefinition):
    entry_statuses: frozenset[str] = frozenset()
    success_status: str = "downloaded"
    cancel_status: str = "manual_review"
    entry_error_codes: frozenset[str] = frozenset()
    entry_remark_tokens: frozenset[str] = frozenset()
    auto_start: bool = False


VIDEO_ARCHIVE_PHASES = frozenset(
    {
        "awaiting_torrent_load",
        "loading_torrent_options",
        "awaiting_torrent_selection",
        "torrent_submit_queued",
        "submitting_torrents",
        "downloading",
        "checking_downloads",
        "extracting",
        "converting",
        "packing",
        "ready",
        "failed",
        "cancelling",
        "cancelled",
    }
)

VIDEO_ARCHIVE = VideoWorkflowDefinition(
    kind=VIDEO_ARCHIVE_KIND,
    label="视频种子下载与整合",
    entry_statuses=frozenset({"manual_review"}),
    initial_phase="awaiting_torrent_load",
    success_status="downloaded",
    cancel_status="manual_review",
    entry_error_codes=frozenset({"video_torrent"}),
    entry_remark_tokens=frozenset({"video_torrent"}),
    operations={
        LOAD_TORRENT_OPTIONS: OperationDefinition(
            LOAD_TORRENT_OPTIONS,
            frozenset({"awaiting_torrent_load", "awaiting_torrent_selection", "failed"}),
            "loading_torrent_options",
        ),
        SUBMIT_SELECTED_TORRENTS: OperationDefinition(
            SUBMIT_SELECTED_TORRENTS,
            frozenset({"torrent_submit_queued", "failed"}),
            "submitting_torrents",
            effect="verify",
        ),
        CHECK_AND_COMPOSE: OperationDefinition(
            CHECK_AND_COMPOSE,
            frozenset({"downloading", "failed"}),
            "checking_downloads",
            lease_seconds=24 * 60 * 60,
        ),
        CANCEL_VIDEO_ARCHIVE: OperationDefinition(
            CANCEL_VIDEO_ARCHIVE,
            VIDEO_ARCHIVE_PHASES - {"ready", "cancelled", "cancelling"},
            "cancelling",
            cancellation=True,
            effect="verify",
        ),
        CLEANUP_SOURCES_AFTER_COMPLETE: OperationDefinition(
            CLEANUP_SOURCES_AFTER_COMPLETE,
            frozenset({"ready"}),
            "ready",
            allowed_statuses=frozenset({"completed"}),
            failure_phase="ready",
            affects_workflow=False,
            effect="verify",
        ),
    },
)
