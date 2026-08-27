from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import AppConfig, load_config, load_video_archive_config
from ..db.models import (
    EventLog,
    MangaRecord,
    SpecialJob,
    SpecialWorkflow,
)
from ..db.repository import utcnow
from ..domain.states import Status, transition_target
from .handlers import module_capability
from .registry import (
    CANCEL_VIDEO_ARCHIVE,
    CHECK_AND_COMPOSE,
    LOAD_TORRENT_OPTIONS,
    SUBMIT_SELECTED_TORRENTS,
    VIDEO_ARCHIVE,
    eligible_workflow_definitions,
    get_operation,
)
from .remarks import restore_entry_error, sync_remark
from .repository import SpecialRepository


class SpecialServiceError(Exception):
    status_code = 400


class SpecialNotFound(SpecialServiceError):
    status_code = 404


class SpecialConflict(SpecialServiceError):
    status_code = 409


class SpecialInvalidRequest(SpecialServiceError):
    status_code = 400


@dataclass(frozen=True)
class ModuleHealth:
    available: bool
    reason: str | None
    enabled: bool


@dataclass(frozen=True)
class SpecialEntry:
    kind: str
    label: str


@dataclass(frozen=True)
class BatchDispatchResult:
    found: int
    queued: int
    skipped: int


def special_module_health(kind: str, config_dir: str | Path) -> ModuleHealth:
    """Return scheduling/configuration availability without dependency probes."""

    try:
        _, supervisor, _, _ = load_config(config_dir)
    except (OSError, TypeError, ValueError) as exc:
        return ModuleHealth(False, str(exc), False)
    if not supervisor.special_processing_enabled or not supervisor.modules.get(
        "special_processing", True
    ):
        return ModuleHealth(False, "Supervisor 已禁用特殊处理模块", False)
    capability = module_capability(kind, config_dir)
    if not capability.enabled:
        return ModuleHealth(False, capability.reason, False)
    return ModuleHealth(True, None, True)


def video_archive_health(config_dir: str | Path) -> ModuleHealth:
    """Compatibility name for the module's lightweight enabled state."""

    return special_module_health(VIDEO_ARCHIVE.kind, config_dir)


def special_entry_for_manga(
    manga: MangaRecord,
    config_dir: str | Path,
) -> SpecialEntry | None:
    """Return a lightweight extension entry without touching external dependencies."""

    matches = eligible_workflow_definitions(
        status=manga.status,
        error_code=manga.last_error_code,
    )
    for definition in matches:
        if special_module_health(definition.kind, config_dir).available:
            return SpecialEntry(definition.kind, definition.label)
    return None


def active_special_workflow(session: Session, manga_id: str) -> SpecialWorkflow | None:
    return session.scalar(
        select(SpecialWorkflow).where(
            SpecialWorkflow.manga_id == manga_id,
            SpecialWorkflow.status == "active",
        )
    )


class SpecialWorkflowService:
    def __init__(
        self,
        session: Session,
        *,
        actor: str,
        config_dir: str | Path,
        app_config: AppConfig,
        trigger_source: str = "web",
    ) -> None:
        self.session = session
        self.actor = actor
        self.config_dir = Path(config_dir)
        self.app_config = app_config
        self.trigger_source = trigger_source
        self.repository = SpecialRepository(session, timezone=app_config.timezone)

    def start_for_manga(
        self,
        manga_id: str,
        *,
        row_version: int,
        load_options: bool = True,
    ) -> SpecialWorkflow:
        """Resolve an enabled extension from database markers and enter it."""

        manga = self.session.scalar(
            select(MangaRecord).where(MangaRecord.manga_id == manga_id).with_for_update()
        )
        if manga is None:
            raise SpecialNotFound("档案不存在")
        self._require_version(manga.row_version, row_version)
        matches = eligible_workflow_definitions(
            status=manga.status,
            error_code=manga.last_error_code,
        )
        enabled = [
            definition
            for definition in matches
            if special_module_health(definition.kind, self.config_dir).available
        ]
        if not enabled:
            raise SpecialInvalidRequest("当前档案没有已启用且适用的特殊处理模块")
        if len(enabled) != 1:
            raise SpecialConflict("当前档案匹配多个特殊处理模块，需要先选择具体模块")
        definition = enabled[0]
        if definition.kind != VIDEO_ARCHIVE.kind:
            raise SpecialInvalidRequest("当前特殊处理模块尚未实现 Web 入口")
        return self._start_video_archive_locked(manga, load_options=load_options)

    def start_video_archive(
        self,
        manga_id: str,
        *,
        row_version: int,
        load_options: bool,
    ) -> SpecialWorkflow:
        health = video_archive_health(self.config_dir)
        if not health.available:
            raise SpecialInvalidRequest(f"视频特殊处理当前不可用：{health.reason}")
        manga = self.session.scalar(
            select(MangaRecord).where(MangaRecord.manga_id == manga_id).with_for_update()
        )
        if manga is None:
            raise SpecialNotFound("档案不存在")
        self._require_version(manga.row_version, row_version)
        if manga.status not in VIDEO_ARCHIVE.entry_statuses:
            raise SpecialInvalidRequest("只有人工复核状态可以进入视频档案特殊处理")
        return self._start_video_archive_locked(manga, load_options=load_options)

    def _start_video_archive_locked(
        self,
        manga: MangaRecord,
        *,
        load_options: bool,
    ) -> SpecialWorkflow:
        module = load_video_archive_config(self.config_dir)
        if manga.active_attempt_id is not None or manga.lease_owner or manga.lease_token:
            raise SpecialConflict("档案仍有普通任务或租约，不能进入特殊处理")
        if self.repository.active_for_manga(manga.manga_id) is not None:
            raise SpecialConflict("档案已经有活动的特殊工作流")
        now = utcnow()
        previous = manga.status
        payload = {
            "entry": {
                "reason": "video_torrent_detected",
                "source_error_code": manga.last_error_code,
                "source_error_operation": manga.last_error_operation,
                "source_error_detail": manga.last_error_detail,
            },
            "config_snapshot": module.result_snapshot(),
            "torrent_snapshot": None,
            "selection": None,
            "torrents": [],
            "final_artifact": None,
        }
        workflow = SpecialWorkflow(
            manga_id=manga.manga_id,
            kind=VIDEO_ARCHIVE.kind,
            status="active",
            phase=VIDEO_ARCHIVE.initial_phase,
            resume_status=manga.status,
            payload=payload,
            progress={"message": VIDEO_ARCHIVE.initial_phase},
            row_version=0,
            created_by=self.actor,
            created_at=now,
            updated_at=now,
        )
        self.session.add(workflow)
        self.session.flush()
        manga.status = transition_target(manga.status, "special_start").value
        manga.status_updated_at = manga.updated_at = now
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.app_config.timezone)
        self._event(
            workflow,
            "special_start",
            operation=None,
            from_status=previous,
            to_status=manga.status,
            detail={"load_options": load_options, "source_error_code": manga.last_error_code},
        )
        if load_options:
            self.repository.queue_job(
                workflow,
                LOAD_TORRENT_OPTIONS,
                trigger_source=self.trigger_source,
                requested_by=self.actor,
            )
        return workflow

    def queue_load(self, workflow_id: int, *, row_version: int) -> SpecialJob:
        self._require_module_enabled()
        workflow, manga = self._locked_workflow(workflow_id)
        self._require_version(workflow.row_version, row_version)
        if (
            workflow.phase
            not in get_operation(VIDEO_ARCHIVE.kind, LOAD_TORRENT_OPTIONS).allowed_phases
        ):
            raise SpecialInvalidRequest("当前阶段不能加载 Torrent 列表")
        job, _ = self.repository.queue_job(
            workflow,
            LOAD_TORRENT_OPTIONS,
            trigger_source=self.trigger_source,
            requested_by=self.actor,
        )
        manga.updated_at = utcnow()
        return job

    def select_torrents(
        self,
        workflow_id: int,
        *,
        row_version: int,
        image_choice_id: str,
        video_choice_id: str,
        confirmed_warnings: Iterable[str],
    ) -> SpecialJob:
        self._require_module_enabled()
        workflow, manga = self._locked_workflow(workflow_id)
        self._require_version(workflow.row_version, row_version)
        if workflow.phase != "awaiting_torrent_selection":
            raise SpecialInvalidRequest("当前阶段不能提交 Torrent 选择")
        if not image_choice_id or not video_choice_id or image_choice_id == video_choice_id:
            raise SpecialInvalidRequest("必须选择两个不同的图片和视频 Torrent")
        payload = dict(workflow.payload or {})
        snapshot = dict(payload.get("torrent_snapshot") or {})
        choices = {
            str(item.get("choice_id")): item
            for item in snapshot.get("choices", [])
            if isinstance(item, dict)
        }
        if image_choice_id not in choices or video_choice_id not in choices:
            raise SpecialConflict("Torrent 候选已经变化，请重新加载后再选择")
        selected = {"image": choices[image_choice_id], "video": choices[video_choice_id]}
        required = {
            f"{role}:{warning}"
            for role, choice in selected.items()
            for warning in choice.get("warnings", [])
        }
        confirmed = {str(value) for value in confirmed_warnings}
        if not required.issubset(confirmed):
            raise SpecialInvalidRequest("风险 Torrent 必须逐项确认后才能提交")
        now = utcnow()
        payload["selection"] = {
            "image_choice_id": image_choice_id,
            "video_choice_id": video_choice_id,
            "confirmed_warnings": sorted(confirmed),
            "selected_at": now.isoformat(),
            "selected_by": self.actor,
        }
        payload.pop("retry_operation", None)
        workflow.payload = payload
        workflow.phase = "torrent_submit_queued"
        workflow.progress = {"message": "torrent_submit_queued"}
        workflow.error_code = None
        workflow.error_detail = None
        workflow.updated_at = now
        workflow.row_version += 1
        manga.updated_at = now
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.app_config.timezone)
        job, _ = self.repository.queue_job(
            workflow,
            SUBMIT_SELECTED_TORRENTS,
            trigger_source=self.trigger_source,
            requested_by=self.actor,
        )
        self._event(
            workflow,
            "special_selection_confirmed",
            operation=SUBMIT_SELECTED_TORRENTS,
            detail={
                "image_choice_id": image_choice_id,
                "video_choice_id": video_choice_id,
                "confirmed_warnings": sorted(confirmed),
                "job_id": job.id,
            },
        )
        return job

    def queue_check(self, workflow_id: int, *, row_version: int) -> tuple[SpecialJob, bool]:
        self._require_module_enabled()
        workflow, _ = self._locked_workflow(workflow_id)
        self._require_version(workflow.row_version, row_version)
        if workflow.phase != "downloading":
            raise SpecialInvalidRequest("只有下载中的档案可以立即检查")
        return self.repository.queue_job(
            workflow,
            CHECK_AND_COMPOSE,
            trigger_source=self.trigger_source,
            requested_by=self.actor,
        )

    def dispatch_ready_checks(self) -> BatchDispatchResult:
        self._require_module_enabled()
        workflows = list(
            self.session.scalars(
                select(SpecialWorkflow)
                .where(
                    SpecialWorkflow.kind == VIDEO_ARCHIVE.kind,
                    SpecialWorkflow.status == "active",
                    SpecialWorkflow.phase == "downloading",
                )
                .order_by(SpecialWorkflow.updated_at, SpecialWorkflow.id)
                .with_for_update(skip_locked=True)
            )
        )
        queued = 0
        skipped = 0
        for workflow in workflows:
            try:
                # Keep a duplicate/racing row local to this workflow.  One
                # malformed or concurrently queued item must not roll back the
                # independent jobs already created for the rest of the batch.
                with self.session.begin_nested():
                    _, created = self.repository.queue_job(
                        workflow,
                        CHECK_AND_COMPOSE,
                        trigger_source=self.trigger_source,
                        requested_by=self.actor,
                    )
            except (IntegrityError, ValueError) as exc:
                skipped += 1
                self._event(
                    workflow,
                    "special_batch_item_skipped",
                    operation=CHECK_AND_COMPOSE,
                    error_code="special_batch_item_conflict",
                    detail={"error_type": type(exc).__name__},
                )
                continue
            queued += int(created)
            skipped += int(not created)
        self.session.add(
            EventLog(
                manga_id=None,
                component="special_processing",
                event_type="special_batch_dispatched",
                operation=CHECK_AND_COMPOSE,
                actor=self.actor,
                detail={"found": len(workflows), "queued": queued, "skipped": skipped},
            )
        )
        return BatchDispatchResult(len(workflows), queued, skipped)

    def retry(self, workflow_id: int, *, row_version: int) -> SpecialJob:
        self._require_module_enabled()
        workflow, _ = self._locked_workflow(workflow_id)
        self._require_version(workflow.row_version, row_version)
        if workflow.phase != "failed":
            raise SpecialInvalidRequest("只有失败的特殊工作流可以重试")
        operation = str((workflow.payload or {}).get("retry_operation", ""))
        if not operation or operation == CANCEL_VIDEO_ARCHIVE:
            raise SpecialInvalidRequest("当前失败没有可重试的处理阶段")
        get_operation(workflow.kind, operation)
        job, _ = self.repository.queue_job(
            workflow,
            operation,
            trigger_source=self.trigger_source,
            requested_by=self.actor,
        )
        return job

    def cancel(self, workflow_id: int, *, row_version: int) -> SpecialJob:
        workflow, manga = self._locked_workflow(workflow_id)
        self._require_version(workflow.row_version, row_version)
        running = self.session.scalar(
            select(SpecialJob).where(
                SpecialJob.workflow_id == workflow.id,
                SpecialJob.status == "running",
            )
        )
        if running is not None:
            payload = dict(workflow.payload or {})
            if not payload.get("cancel_requested"):
                now = utcnow()
                payload["cancel_requested"] = {
                    "requested_at": now.isoformat(),
                    "requested_by": self.actor,
                }
                workflow.payload = payload
                workflow.progress = {"message": "cancel_requested"}
                workflow.row_version += 1
                workflow.updated_at = now
                running.progress = {"message": "cancel_requested"}
                manga.row_version += 1
                manga.updated_at = now
                sync_remark(manga, workflow, timezone=self.app_config.timezone)
                self._event(
                    workflow,
                    "special_cancel_requested",
                    operation=running.operation,
                    detail={"job_id": running.id},
                )
            return running
        self._require_module_enabled()
        queued = list(
            self.session.scalars(
                select(SpecialJob).where(
                    SpecialJob.workflow_id == workflow.id,
                    SpecialJob.status == "queued",
                )
            )
        )
        now = utcnow()
        for job in queued:
            job.status = "cancelled"
            job.finished_at = now
        # The partial unique index covers queued/running operations.  Flush
        # the cancellations before inserting the cleanup operation so this is
        # reliable on PostgreSQL as well as SQLite.
        self.session.flush()
        workflow.row_version += 1
        workflow.updated_at = now
        manga.row_version += 1
        manga.updated_at = now
        sync_remark(manga, workflow, timezone=self.app_config.timezone)
        job, _ = self.repository.queue_job(
            workflow,
            CANCEL_VIDEO_ARCHIVE,
            trigger_source=self.trigger_source,
            requested_by=self.actor,
        )
        return job

    def release_expired_job(
        self,
        workflow_id: int,
        *,
        row_version: int,
        reason: str,
        confirmed: bool,
    ) -> SpecialJob:
        workflow, manga = self._locked_workflow(workflow_id)
        self._require_version(workflow.row_version, row_version)
        if not confirmed or not reason.strip():
            raise SpecialInvalidRequest("必须确认旧进程已经停止并填写原因")
        job = self.session.scalar(
            select(SpecialJob)
            .where(
                SpecialJob.workflow_id == workflow.id,
                SpecialJob.status == "running",
            )
            .order_by(SpecialJob.started_at.desc())
            .with_for_update()
        )
        now = utcnow()
        if job is None or job.lease_until is None or job.lease_until >= now:
            raise SpecialConflict("当前没有可解除的过期特殊任务租约")
        job.status = "abandoned"
        job.finished_at = now
        job.error_code = "lease_released"
        job.error_detail = reason.strip()
        job.lease_token = None
        job.lease_owner = None
        job.lease_until = None
        payload = dict(workflow.payload or {})
        payload["retry_operation"] = job.operation
        workflow.payload = payload
        workflow.phase = "failed"
        workflow.error_code = "lease_released"
        workflow.error_detail = reason.strip()
        workflow.row_version += 1
        workflow.updated_at = now
        manga.last_error_operation = "special_processing"
        manga.last_error_code = "lease_released"
        manga.last_error_detail = reason.strip()
        manga.last_error_at = now
        manga.row_version += 1
        manga.updated_at = now
        sync_remark(manga, workflow, timezone=self.app_config.timezone)
        self._event(
            workflow,
            "special_lease_released",
            operation=job.operation,
            error_code="lease_released",
            detail={"job_id": job.id, "reason": reason.strip()},
        )
        return job

    def exit_without_cleanup(
        self,
        workflow_id: int,
        *,
        row_version: int,
        reason: str,
        confirmed: bool,
    ) -> SpecialWorkflow:
        workflow, manga = self._locked_workflow(workflow_id)
        self._require_version(workflow.row_version, row_version)
        if not confirmed or not reason.strip():
            raise SpecialInvalidRequest("必须确认保留外部资源并填写退出原因")
        running = self.session.scalar(
            select(SpecialJob.id).where(
                SpecialJob.workflow_id == workflow.id,
                SpecialJob.status == "running",
            )
        )
        if running is not None:
            raise SpecialConflict("特殊任务仍在运行，不能直接退出")
        now = utcnow()
        for job in self.session.scalars(
            select(SpecialJob).where(
                SpecialJob.workflow_id == workflow.id,
                SpecialJob.status == "queued",
            )
        ):
            job.status = "cancelled"
            job.finished_at = now
        payload = dict(workflow.payload or {})
        payload["source_cleanup"] = "retained_on_forced_exit"
        payload["exit_reason"] = reason.strip()
        workflow.payload = payload
        workflow.status = "cancelled"
        workflow.phase = "cancelled"
        workflow.completed_at = now
        workflow.updated_at = now
        workflow.error_code = None
        workflow.error_detail = None
        workflow.row_version += 1
        previous = manga.status
        manga.status = workflow.resume_status or Status.MANUAL_REVIEW.value
        manga.status_updated_at = manga.updated_at = now
        restore_entry_error(manga, workflow, restored_at=now)
        manga.row_version += 1
        sync_remark(manga, workflow, timezone=self.app_config.timezone)
        self._event(
            workflow,
            "special_forced_exit",
            operation=None,
            from_status=previous,
            to_status=manga.status,
            detail={"reason": reason.strip(), "external_resources_retained": True},
        )
        return workflow

    def _locked_workflow(self, workflow_id: int) -> tuple[SpecialWorkflow, MangaRecord]:
        workflow = self.session.scalar(
            select(SpecialWorkflow).where(SpecialWorkflow.id == workflow_id).with_for_update()
        )
        if workflow is None:
            raise SpecialNotFound("特殊工作流不存在")
        if workflow.kind != VIDEO_ARCHIVE.kind or workflow.status != "active":
            raise SpecialInvalidRequest("特殊工作流已经结束或类型不匹配")
        manga = self.session.scalar(
            select(MangaRecord).where(MangaRecord.manga_id == workflow.manga_id).with_for_update()
        )
        if manga is None:
            raise SpecialNotFound("特殊工作流所属档案不存在")
        if manga.status != Status.SPECIAL_PROCESSING.value:
            raise SpecialConflict("档案状态与特殊工作流不一致")
        return workflow, manga

    def _require_module_enabled(self) -> None:
        health = video_archive_health(self.config_dir)
        if not health.available:
            raise SpecialInvalidRequest(f"视频特殊处理当前不可用：{health.reason}")

    @staticmethod
    def _require_version(current: int, requested: int) -> None:
        if current != requested:
            raise SpecialConflict("页面数据已经变化，请刷新后重试")

    def _event(
        self,
        workflow: SpecialWorkflow,
        event_type: str,
        *,
        operation: str | None,
        from_status: str | None = None,
        to_status: str | None = None,
        error_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            EventLog(
                manga_id=workflow.manga_id,
                component="special_processing",
                event_type=event_type,
                operation=operation,
                from_status=from_status,
                to_status=to_status,
                error_code=error_code,
                actor=self.actor,
                detail={"workflow_id": workflow.id, **(detail or {})},
            )
        )


def special_workflow_detail(session: Session, workflow_id: int) -> dict[str, Any]:
    workflow = session.get(SpecialWorkflow, workflow_id)
    if workflow is None:
        raise SpecialNotFound("特殊工作流不存在")
    manga = session.get(MangaRecord, workflow.manga_id)
    jobs = list(
        session.scalars(
            select(SpecialJob)
            .where(SpecialJob.workflow_id == workflow.id)
            .order_by(desc(SpecialJob.created_at), desc(SpecialJob.id))
        )
    )
    candidate_events = list(
        session.scalars(
            select(EventLog)
            .where(
                EventLog.manga_id == workflow.manga_id,
                EventLog.component == "special_processing",
            )
            .order_by(desc(EventLog.created_at), desc(EventLog.id))
            .limit(500)
        )
    )
    events = [
        event
        for event in candidate_events
        if (event.detail or {}).get("workflow_id") == workflow.id
    ][:100]
    running = next((job for job in jobs if job.status == "running"), None)
    expired_job = (
        running
        if running and running.lease_until is not None and running.lease_until < utcnow()
        else None
    )
    payload = dict(workflow.payload or {})
    return {
        "workflow": workflow,
        "manga": manga,
        "jobs": jobs,
        "events": events,
        "payload": payload,
        "choices": list((payload.get("torrent_snapshot") or {}).get("choices", [])),
        "torrents": list(payload.get("torrents", [])),
        "expired_job": expired_job,
    }


def list_video_workflows(session: Session, *, limit: int = 200) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(SpecialWorkflow)
            .where(SpecialWorkflow.kind == VIDEO_ARCHIVE.kind)
            .order_by(desc(SpecialWorkflow.updated_at), desc(SpecialWorkflow.id))
            .limit(max(1, min(limit, 500)))
        )
    )
    counts = {
        status: int(
            session.scalar(
                select(func.count())
                .select_from(SpecialJob)
                .join(SpecialWorkflow, SpecialWorkflow.id == SpecialJob.workflow_id)
                .where(
                    SpecialJob.status == status,
                    SpecialWorkflow.kind == VIDEO_ARCHIVE.kind,
                )
            )
            or 0
        )
        for status in ("queued", "running", "failed")
    }
    return {"workflows": rows, "job_counts": counts}
