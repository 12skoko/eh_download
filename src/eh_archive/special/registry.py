"""Supported registry and video declaration imports."""

from .core.registry import WORKFLOW_REGISTRY, get_operation, get_workflow_definition
from .modules.video_archive.definition import *


def eligible_workflow_definitions(*, status, error_code, remark=None):
    from .catalog import load_modules

    load_modules()
    return tuple(
        d
        for d in WORKFLOW_REGISTRY.values()
        if status in getattr(d, "entry_statuses", ())
        and (
            (not d.entry_error_codes and not d.entry_remark_tokens)
            or (error_code or "").casefold() in d.entry_error_codes
            or any(t.casefold() in (remark or "").casefold() for t in d.entry_remark_tokens)
        )
    )


__all__ = [
    "CANCEL_VIDEO_ARCHIVE",
    "CHECK_AND_COMPOSE",
    "CLEANUP_SOURCES_AFTER_COMPLETE",
    "LOAD_TORRENT_OPTIONS",
    "SUBMIT_SELECTED_TORRENTS",
    "VIDEO_ARCHIVE",
    "VIDEO_ARCHIVE_KIND",
    "WORKFLOW_REGISTRY",
    "eligible_workflow_definitions",
    "get_operation",
    "get_workflow_definition",
]
