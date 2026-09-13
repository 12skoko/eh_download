from sqlalchemy import select

from ...config import load_config
from ...db.models import EventLog, SpecialJob, SpecialWorkflow
from ...db.repository import utcnow
from ..handlers import module_capability
from .events import workflow_event_filter
from .registry import get_operation, get_workflow_definition
from .repository import SpecialRepository, aware


class SpecialServiceError(ValueError):
    status_code = 400


class SpecialNotFound(SpecialServiceError):
    status_code = 404


class SpecialConflict(SpecialServiceError):
    status_code = 409


class SpecialInvalidRequest(SpecialServiceError):
    pass


class ModuleService:
    def __init__(self, session, *, actor, config_dir, app_config, trigger_source="web"):
        self.session, self.actor, self.config_dir = session, actor, config_dir
        self.app_config, self.trigger_source = app_config, trigger_source
        self.repository = SpecialRepository(session, timezone=app_config.timezone)
        self.repository.scheduling_lock()

    def enabled(self, kind):
        _, supervisor, _, _ = load_config(self.config_dir)
        capability = module_capability(kind, self.config_dir)
        if (
            not capability.enabled
            or not supervisor.special_processing_enabled
            or not supervisor.modules.get("special_processing", True)
        ):
            raise SpecialInvalidRequest(capability.reason or "特殊处理已禁用")

    def create(self, kind, inputs=None):
        self.enabled(kind)
        definition = get_workflow_definition(kind)
        if definition.create is None:
            raise SpecialInvalidRequest("模块没有创建入口")
        return definition.create(self, inputs or {})

    def locked(self, workflow_id, row_version):
        workflow = self.session.scalar(
            select(SpecialWorkflow).where(SpecialWorkflow.id == workflow_id).with_for_update()
        )
        if workflow is None:
            raise SpecialNotFound("特殊工作流不存在")
        if workflow.row_version != row_version:
            raise SpecialConflict("页面数据已经变化，请刷新后重试")
        return workflow

    def action(self, workflow_id, action, *, row_version, inputs=None):
        workflow = self.locked(workflow_id, row_version)
        definition = get_workflow_definition(workflow.kind)
        inputs = inputs or {}
        if action in definition.actions:
            return definition.actions[action](self, workflow, inputs)
        if action == "migrate-data":
            if any(j.status in {"queued", "running"} for j in workflow.jobs):
                raise SpecialConflict("数据升级前必须停止活动任务")
            payload = dict(workflow.payload or {})
            version = workflow.schema_version
            while version < definition.schema_version:
                migrate = definition.migrations.get(version)
                if migrate is None:
                    raise SpecialInvalidRequest("模块未提供此版本的迁移路径")
                payload = migrate(payload)
                version += 1
            if version != definition.schema_version:
                raise SpecialInvalidRequest("不能将数据降级")
            workflow.payload, workflow.schema_version = payload, version
            workflow.row_version += 1
            self.repository._event(
                workflow,
                "special_data_migrated",
                operation=None,
                actor=self.actor,
                detail={"schema_version": version},
            )
            return workflow
        if action == "release-expired":
            if inputs.get("confirmed") is not True or not str(inputs.get("reason", "")).strip():
                raise SpecialInvalidRequest("必须确认旧进程已停止并填写原因")
            job = next((j for j in workflow.jobs if j.status == "running"), None)
            if job is None or job.lease_until is None or aware(job.lease_until) >= utcnow():
                raise SpecialConflict("没有可解除的过期任务租约")
            op = get_operation(workflow.kind, job.operation)
            if op.effect == "verify" and job.external_effect_started_at:
                raise SpecialConflict("模块必须先核实外部副作用")
            self.repository.release_resources(workflow, job=job)
            job.status, job.finished_at = "abandoned", utcnow()
            job.lease_token = job.lease_owner = job.lease_until = None
            job.error_code, job.error_detail = "lease_released", str(inputs["reason"])[:2000]
            if op.affects_workflow:
                workflow.phase = op.failure_phase
            workflow.error_code, workflow.error_detail = job.error_code, job.error_detail
            self.repository._changed(workflow, job, "failed")
            self.repository._event(
                workflow,
                "special_lease_released",
                operation=job.operation,
                actor=self.actor,
                detail={"job_id": job.id, "reason": job.error_detail},
            )
            return job
        if action == "cancel":
            if workflow.status != "active":
                raise SpecialInvalidRequest("工作流已结束")
            workflow.cancel_requested_at = utcnow()
            workflow.row_version += 1
            jobs = list(
                self.session.scalars(
                    select(SpecialJob).where(
                        SpecialJob.workflow_id == workflow.id,
                        SpecialJob.status.in_(("queued", "running")),
                    )
                )
            )
            for job in jobs:
                if job.status == "queued":
                    job.status, job.finished_at = "cancelled", utcnow()
            self.session.flush()
            if not any(j.status == "running" for j in jobs):
                cancellation = next(
                    (op for op in definition.operations.values() if op.cancellation), None
                )
                if cancellation:
                    self.enabled(workflow.kind)
                    self.repository.queue_job(
                        workflow,
                        cancellation.name,
                        trigger_source=self.trigger_source,
                        requested_by=self.actor,
                    )
                elif any(j.external_effect_started_at for j in workflow.jobs):
                    raise SpecialConflict("存在未核实的外部副作用，需模块恢复操作")
                else:
                    workflow.status, workflow.phase, workflow.completed_at = (
                        "cancelled",
                        "cancelled",
                        utcnow(),
                    )
                    self.repository.release_resources(workflow, all_scopes=True)
            self.repository._event(
                workflow, "special_cancel_requested", operation=None, actor=self.actor
            )
            if workflow.status == "cancelled":
                self.repository._event(
                    workflow, "special_cancelled", operation=None, actor=self.actor
                )
            return workflow
        self.enabled(workflow.kind)
        if action == "retry":
            if workflow.phase != "failed":
                raise SpecialInvalidRequest("当前工作流不可重试")
            failed = self.session.scalar(
                select(SpecialJob)
                .where(
                    SpecialJob.workflow_id == workflow.id,
                    SpecialJob.status.in_(("failed", "abandoned")),
                )
                .order_by(SpecialJob.id.desc())
                .limit(1)
            )
            if failed is None:
                raise SpecialInvalidRequest("没有可重试的失败任务")
            operation = failed.operation
        else:
            operation = action
        op = get_operation(workflow.kind, operation)
        clean_input = op.validate_input(inputs)
        if clean_input:
            workflow.payload = {
                **workflow.payload,
                "operation_inputs": {
                    **workflow.payload.get("operation_inputs", {}),
                    operation: clean_input,
                },
            }
        result = self.repository.queue_job(
            workflow, operation, trigger_source=self.trigger_source, requested_by=self.actor
        )
        if result[1]:
            workflow.row_version += 1
        return result[0]


def workflow_detail(session, workflow_id, *, page=1):
    workflow = session.get(SpecialWorkflow, workflow_id)
    if workflow is None:
        raise SpecialNotFound("特殊工作流不存在")
    jobs = list(
        session.scalars(
            select(SpecialJob)
            .where(SpecialJob.workflow_id == workflow_id)
            .order_by(SpecialJob.id.desc())
            .offset((max(1, page) - 1) * 50)
            .limit(50)
        )
    )
    events = list(
        session.scalars(
            select(EventLog)
            .where(
                EventLog.component == "special_processing",
                workflow_event_filter(session, workflow_id),
            )
            .order_by(EventLog.created_at.desc(), EventLog.id.desc())
            .offset((max(1, page) - 1) * 100)
            .limit(100)
        )
    )
    try:
        definition = get_workflow_definition(workflow.kind)
        execution_reason = (
            None
            if workflow.schema_version in definition.readable_versions
            else "工作流数据版本不兼容"
        )
    except ValueError:
        execution_reason = "模块未安装；只能查看历史和输出"
    expired = next(
        (
            job
            for job in jobs
            if job.status == "running" and job.lease_until and aware(job.lease_until) < utcnow()
        ),
        None,
    )
    return {
        "workflow": workflow,
        "jobs": jobs,
        "events": events,
        "payload": workflow.payload or {},
        "page": max(1, page),
        "execution_reason": execution_reason,
        "expired_job": expired,
    }
