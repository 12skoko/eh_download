"""Composition root. New modules are registered here, never in the scheduler."""

from collections.abc import Callable
from dataclasses import dataclass, replace

from .core.registry import register
from .handlers import ModuleCapability


@dataclass(frozen=True)
class ModuleRegistration:
    definition: object
    executor: Callable
    capability: Callable
    description: str
    template_name: str
    load_dashboard: Callable
    load_detail: Callable | None = None
    detail_template: str = "special/generic_detail.html"
    panel_template: str = "special/_generic_panel.html"
    install_routes: Callable | None = None

    @property
    def kind(self):
        return self.definition.kind

    @property
    def label(self):
        return self.definition.label

    @property
    def url(self):
        return f"/special/modules/{self.kind}"


MODULES = {}
_loaded = False


def _video_executor(database, config_dir, claim):
    from .modules.video_archive.video import VideoArchiveExecutor

    return VideoArchiveExecutor(database, config_dir=config_dir, claim=claim)


def _video_capability(config_dir):
    from ..config import load_video_archive_config

    config = load_video_archive_config(config_dir)
    return ModuleCapability(
        "video_archive",
        config.enabled,
        config.work.max_concurrency,
        None if config.enabled else "video_archive.enabled=false",
    )


def _video_dashboard(session, *, page=1):
    from .modules.video_archive.service import list_video_workflows

    return list_video_workflows(session, page=page)


def _video_detail(session, workflow_id):
    from .modules.video_archive.service import special_workflow_detail

    return special_workflow_detail(session, workflow_id)


def _video_create(service, inputs):
    from .modules.video_archive.service import SpecialWorkflowService

    return SpecialWorkflowService(
        service.session,
        actor=service.actor,
        config_dir=service.config_dir,
        app_config=service.app_config,
        trigger_source=service.trigger_source,
    ).start_video_archive(**inputs)


def _video_action(method):
    def dispatch(service, workflow, inputs):
        from .modules.video_archive.service import SpecialWorkflowService

        video = SpecialWorkflowService(
            service.session,
            actor=service.actor,
            config_dir=service.config_dir,
            app_config=service.app_config,
            trigger_source=service.trigger_source,
        )
        return getattr(video, method)(workflow.id, row_version=workflow.row_version, **inputs)

    return dispatch


def _compare_executor(database, config_dir, claim):
    from .modules.lanraragi_compare.module import CompareExecutor

    return CompareExecutor(database, config_dir=config_dir, claim=claim)


def _compare_capability(config_dir):
    from .modules.lanraragi_compare.config import load_compare_config

    config = load_compare_config(config_dir)
    return ModuleCapability(
        "lanraragi_compare",
        config.enabled,
        config.max_concurrency,
        None if config.enabled else "lanraragi_compare.enabled=false",
    )


def load_modules():
    global _loaded
    if _loaded:
        return
    from .modules.lanraragi_compare.module import DEFINITION, dashboard, detail
    from .modules.video_archive.definition import VIDEO_ARCHIVE
    from .modules.video_archive.integration import VideoIntegration

    video = replace(
        VIDEO_ARCHIVE,
        integration=VideoIntegration(),
        create=_video_create,
        failure_phases={"torrent_selection_stale": "awaiting_torrent_selection"},
        actions={
            name: _video_action(method)
            for name, method in {
                "load_torrent_options": "queue_load",
                "submit_selected_torrents": "select_torrents",
                "check_and_compose_if_ready": "queue_check",
                "cleanup_sources_after_complete": "queue_source_cleanup",
                "retry": "retry",
                "cancel": "cancel",
                "cancel_video_archive": "cancel",
                "release-expired": "release_expired_job",
                "exit-without-cleanup": "exit_without_cleanup",
            }.items()
        },
    )
    registrations = (
        ModuleRegistration(
            video,
            _video_executor,
            _video_capability,
            "选择图片与视频 Torrent，等待下载完成后转换并整合为普通归档产物。",
            "special/video_archive.html",
            _video_dashboard,
            _video_detail,
            "special_detail.html",
            "_special_workflow_panel.html",
        ),
        ModuleRegistration(
            DEFINITION,
            _compare_executor,
            _compare_capability,
            "核对 completed 档案与 LANraragi 数字 ID，查看差异、重复及无法解析的档案。",
            "special/lanraragi_compare.html",
            dashboard,
            detail,
        ),
    )
    for module in registrations:
        register(module.definition)
        MODULES[module.kind] = module
    _loaded = True
