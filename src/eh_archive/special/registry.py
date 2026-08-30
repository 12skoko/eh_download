from __future__ import annotations

from dataclasses import dataclass

VIDEO_ARCHIVE_KIND = "video_archive"

LOAD_TORRENT_OPTIONS = "load_torrent_options"
SUBMIT_SELECTED_TORRENTS = "submit_selected_torrents"
CHECK_AND_COMPOSE = "check_and_compose_if_ready"
CANCEL_VIDEO_ARCHIVE = "cancel_video_archive"
CLEANUP_SOURCES_AFTER_COMPLETE = "cleanup_sources_after_complete"


@dataclass(frozen=True)
class OperationDefinition:
    name: str
    allowed_phases: frozenset[str]
    running_phase: str
    lease_seconds: int | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    kind: str
    label: str
    entry_statuses: frozenset[str]
    initial_phase: str
    success_status: str
    cancel_status: str
    operations: dict[str, OperationDefinition]
    entry_error_codes: frozenset[str] = frozenset()
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

VIDEO_ARCHIVE = WorkflowDefinition(
    kind=VIDEO_ARCHIVE_KIND,
    label="视频种子下载与整合",
    entry_statuses=frozenset({"manual_review"}),
    initial_phase="awaiting_torrent_load",
    success_status="downloaded",
    cancel_status="manual_review",
    entry_error_codes=frozenset({"video_torrent"}),
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
        ),
        CLEANUP_SOURCES_AFTER_COMPLETE: OperationDefinition(
            CLEANUP_SOURCES_AFTER_COMPLETE,
            frozenset({"ready"}),
            "ready",
        ),
    },
)

WORKFLOW_REGISTRY = {VIDEO_ARCHIVE_KIND: VIDEO_ARCHIVE}


def get_workflow_definition(kind: str) -> WorkflowDefinition:
    try:
        return WORKFLOW_REGISTRY[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported special workflow kind: {kind}") from exc


def get_operation(kind: str, operation: str) -> OperationDefinition:
    definition = get_workflow_definition(kind)
    try:
        return definition.operations[operation]
    except KeyError as exc:
        raise ValueError(f"unsupported operation {operation!r} for workflow {kind!r}") from exc


def eligible_workflow_definitions(
    *,
    status: str,
    error_code: str | None,
) -> tuple[WorkflowDefinition, ...]:
    """Match extension entry points using persisted archive fields only."""

    normalized_error = (error_code or "").casefold()
    return tuple(
        definition
        for definition in WORKFLOW_REGISTRY.values()
        if status in definition.entry_statuses
        and (
            not definition.entry_error_codes
            or normalized_error in definition.entry_error_codes
        )
    )
