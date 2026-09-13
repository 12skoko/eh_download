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


def special_workflow_detail(session, workflow_id, *, page=1):
    from .catalog import MODULES, load_modules

    load_modules()
    detail = workflow_detail(session, workflow_id, page=page)
    module = MODULES.get(detail["workflow"].kind)
    detail["module_label"] = module.label if module else detail["workflow"].kind
    compatible = module is not None and not detail["execution_reason"]
    if compatible and module.load_detail:
        detail.update(module.load_detail(session, workflow_id))
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
