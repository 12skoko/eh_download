"""Supported application entry points and registered detail dispatch."""

from .core.service import SpecialServiceError, workflow_detail
from .modules.video_archive.service import (
    BatchDispatchResult,
    ModuleHealth,
    SpecialConflict,
    SpecialEntry,
    SpecialInvalidRequest,
    SpecialNotFound,
    SpecialWorkflowService,
    active_special_workflow,
    list_video_workflows,
    special_entry_for_manga,
    special_module_health,
    video_archive_health,
)


def special_workflow_detail(session, workflow_id, *, page=1, jobs_page=None, events_page=None):
    from .catalog import MODULES, load_modules

    load_modules()
    detail = workflow_detail(
        session, workflow_id, page=page, jobs_page=jobs_page, events_page=events_page
    )
    history = {key: detail[key] for key in ("jobs", "events", "history_paging")}
    module = MODULES.get(detail["workflow"].kind)
    detail["module_label"] = module.label if module else detail["workflow"].kind
    compatible = module is not None and not detail["execution_reason"]
    if compatible and module.load_detail:
        detail.update(module.load_detail(session, workflow_id))
    if history["history_paging"] is not None:
        detail.update(history)
    detail["detail_template"] = module.detail_template if compatible else "special/generic_detail.html"
    detail["panel_template"] = module.panel_template if compatible else "special/_generic_panel.html"
    return detail


__all__ = [
    "BatchDispatchResult",
    "ModuleHealth",
    "SpecialConflict",
    "SpecialEntry",
    "SpecialInvalidRequest",
    "SpecialNotFound",
    "SpecialServiceError",
    "SpecialWorkflowService",
    "active_special_workflow",
    "list_video_workflows",
    "special_entry_for_manga",
    "special_module_health",
    "special_workflow_detail",
    "video_archive_health",
]
