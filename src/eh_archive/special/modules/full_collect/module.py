"""Full-collection state machine, registered actions and presentation."""

from sqlalchemy import func, select

from ....config import load_config
from ....db.models import SpecialJob, SpecialWorkflow, SystemControl
from ....db.repository import utcnow
from ...core.contracts import (
    Integration,
    LifecyclePolicy,
    OperationDefinition,
    OperationResult,
    WorkflowDefinition,
)
from ...core.repository import aware
from ...core.service import SpecialConflict, SpecialInvalidRequest
from ...handlers import ModuleCapability
from .boundaries import deadline, initial_scope
from .config import load_full_collect_config, parse_start_at

KIND = "full_collect"
OPERATION = "collect_batch"
PHASE_LABELS = {
    "queued": "等待调度 / 批次间等待",
    "collecting": "采集中",
    "pausing": "正在暂停",
    "paused": "已暂停",
    "cooling": "冷却中",
    "interrupted": "已中断",
    "waiting_repair": "等待修复",
    "completed": "本轮完成",
    "failed": "执行异常",
    "cancelled": "已终止",
}


def active_jobs(session, workflow_id):
    return list(
        session.scalars(
            select(SpecialJob)
            .where(
                SpecialJob.workflow_id == workflow_id,
                SpecialJob.status.in_(("queued", "running")),
            )
            .order_by(SpecialJob.id)
        )
    )


def _others(session, workflow_id=None):
    return list(
        session.scalars(
            select(SpecialWorkflow).where(
                SpecialWorkflow.kind == KIND,
                SpecialWorkflow.status == "active",
                SpecialWorkflow.id != (workflow_id or 0),
            )
        )
    )


def _assert_idle(session, workflow_id=None):
    for other in _others(session, workflow_id):
        if active_jobs(session, other.id) or other.payload.get("intent") == "run":
            raise SpecialConflict("请先暂停当前全量轮次，并等待执行者安全退出")


def _config(service):
    config = load_full_collect_config(service.config_dir)
    if not config.enabled:
        raise SpecialInvalidRequest("full_collect.enabled=false")
    return config


def create(service, inputs):
    config = _config(service)
    _assert_idle(service.session)
    app, _, _, secrets = load_config(service.config_dir)
    scope = initial_scope(config, app, secrets, inputs)
    workflow = service.repository.create(KIND, actor=service.actor, payload={
        "round_type": "id_range", "scope": scope, "cursor": scope["initial_url"],
        "intent": "run", "end_reached": False,
        "counts": {"pages": 0, "requests": 0, "found": 0, "created": 0, "updated": 0, "retries": 0},
        "consecutive_failures": 0, "request_failures": 0, "proxy_offset": 0,
        "failed_proxies": [],
    })
    service.repository.queue_job(
        workflow, OPERATION, trigger_source=service.trigger_source, requested_by=service.actor,
    )
    return workflow


def creation_defaults(config_dir):
    config = load_full_collect_config(config_dir)
    app, _, _, secrets = load_config(config_dir)
    return {
        "collect_default_base": config.base_url,
        "collect_default_account": app.full_collect_session.account,
        "collect_accounts": sorted(secrets.accounts),
    }


def _changed(service, workflow, event):
    workflow.row_version += 1
    workflow.updated_at = utcnow()
    service.repository._event(workflow, event, operation=None, actor=service.actor)


def pause(service, workflow, inputs):
    if inputs or workflow.status != "active":
        raise SpecialInvalidRequest("只有活动轮次可以暂停")
    jobs = active_jobs(service.session, workflow.id)
    for job in jobs:
        if job.status == "queued":
            job.status, job.finished_at = "cancelled", utcnow()
    running = any(job.status == "running" for job in jobs)
    workflow.payload = {**workflow.payload, "intent": "pause", "stop_reason": "user_pause"}
    workflow.phase = "pausing" if running else "paused"
    _changed(service, workflow, "full_collect_pause_requested")
    return workflow


def resume(service, workflow, inputs):
    if inputs or workflow.status != "active":
        raise SpecialInvalidRequest("只有活动轮次可以继续")
    service.enabled(KIND)
    _config(service)
    if active_jobs(service.session, workflow.id):
        raise SpecialConflict("当前批次尚未停止，或已经排队")
    _assert_idle(service.session, workflow.id)
    if workflow.schema_version != 2:
        raise SpecialInvalidRequest("旧日期轮次只能查看、暂停或终止；请按 ID 区间新建轮次")
    workflow.payload = {
        **workflow.payload,
        "intent": "run",
        "stop_reason": "",
        "consecutive_failures": 0,
        "request_failures": 0,
        "failed_proxies": [],
    }
    workflow.phase, workflow.error_code, workflow.error_detail = "queued", None, None
    _changed(service, workflow, "full_collect_resumed")
    service.repository.queue_job(
        workflow,
        OPERATION,
        trigger_source=service.trigger_source,
        requested_by=service.actor,
        next_run_at=deadline(workflow.payload),
    )
    return workflow


def terminate(service, workflow, inputs):
    if inputs != {"confirmed": True}:
        raise SpecialInvalidRequest("必须确认永久终止此轮，已采集档案和检查点会保留")
    if workflow.status != "active":
        raise SpecialInvalidRequest("工作流已结束")
    if workflow.payload.get("intent") != "pause" or active_jobs(service.session, workflow.id):
        raise SpecialConflict("请先暂停此轮，并等待所有批次安全退出")
    # Existing legacy backfills remain visible and must be settled before their parent.
    if any(w.payload.get("history_id") == workflow.id for w in _others(service.session, workflow.id)):
        raise SpecialConflict("请先暂停并终止关联的旧补齐轮次")
    workflow.payload = {**workflow.payload, "intent": "stop", "stop_reason": "user_terminate"}
    workflow.status = workflow.phase = "cancelled"
    workflow.completed_at = utcnow()
    service.repository.release_resources(workflow, all_scopes=True)
    _changed(service, workflow, "full_collect_terminated")
    return workflow


def recover(workflow, jobs, context):
    data = dict(workflow.payload)
    if workflow.schema_version != 2:
        data.update(intent="pause", stop_reason="legacy_id_range_required")
        return OperationResult("paused", payload=data)
    if data.get("end_reached"):
        return OperationResult("completed", payload=data, status="completed")
    if data.get("intent") != "run":
        return OperationResult("paused", payload=data)
    if workflow.phase in {"waiting_repair", "failed"}:
        return None
    config = load_full_collect_config(context.config_dir)
    if not config.resume_after_restart:
        data.update(intent="pause", stop_reason="restart_requires_resume")
        return OperationResult("interrupted", payload=data)
    if any(j.status == "queued" for j in jobs):
        return None  # The original random deadline remains untouched.
    data["stop_reason"] = f"recovered_{context.reason}"
    due = deadline(data, context.now)
    return OperationResult(
        "cooling"
        if parse_start_at(data.get("cooldown_until", "")) and due > context.now
        else "queued",
        payload=data,
        next_operation=OPERATION,
        delay_seconds=(due - context.now).total_seconds(),
    )


class FullCollectIntegration(Integration):
    def validate(self, session, workflow, job):
        # A running job must retain its claim long enough to acknowledge pause.
        return workflow.schema_version == 2 and (
            job.status == "running" or workflow.payload.get("intent") == "run"
        )


DEFINITION = WorkflowDefinition(
    KIND,
    "全量采集",
    "queued",
    {
        OPERATION: OperationDefinition(
            OPERATION,
            frozenset({"queued", "cooling", "interrupted"}),
            "collecting",
            failure_phase="waiting_repair",
        )
    },
    schema_version=2,
    readable_versions=frozenset({1, 2}),
    integration=FullCollectIntegration(),
    create=create,
    actions={
        "pause": pause,
        "resume": resume,
        "retry": resume,
        "cancel": terminate,
        "terminate": terminate,
    },
    lifecycle=LifecyclePolicy(
        cooperative_stop=True,
        fenced_operations=frozenset({OPERATION}),
        recover=recover,
    ),
)


def capability(config_dir):
    config = load_full_collect_config(config_dir)
    return ModuleCapability(
        KIND,
        config.enabled,
        config.max_concurrency,
        None if config.enabled else "full_collect.enabled=false",
    )


def executor(database, config_dir, claim):
    from .executor import FullCollectExecutor

    return FullCollectExecutor(database, config_dir=config_dir, claim=claim)


def dashboard(session, *, page=1):
    query = select(SpecialWorkflow).where(SpecialWorkflow.kind == KIND)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    workflows = list(
        session.scalars(
            query.order_by(SpecialWorkflow.id.desc()).offset((max(1, page) - 1) * 50).limit(50)
        )
    )
    return {
        "workflows": workflows,
        "total": total,
        "page": max(1, page),
        "phase_labels": PHASE_LABELS,
    }


def detail(session, workflow_id):
    workflow = session.get(SpecialWorkflow, workflow_id)
    jobs = active_jobs(session, workflow_id)
    due = next((j.next_run_at for j in jobs if j.status == "queued"), None)
    now = utcnow()
    control = session.get(SystemControl, "supervisor")
    online = bool(control and control.lease_until and aware(control.lease_until) > now)
    cooling = parse_start_at(workflow.payload.get("cooldown_until", ""))
    started = parse_start_at(workflow.payload.get("started_at", ""))
    return {
        "supervisor_online": online,
        "supervisor_state": control.state if control else "offline",
        "wait_seconds": max(0, int((aware(due) - now).total_seconds())) if due else 0,
        "cooldown_seconds": max(0, int((cooling - now).total_seconds())) if cooling else 0,
        "elapsed_seconds": int((now - started).total_seconds()) if started else 0,
        "phase_labels": PHASE_LABELS,
        "current_job": next(iter(jobs), None),
        "next_due": due,
        "legacy_round": workflow.schema_version != 2,
        "can_terminate": workflow.status == "active" and not jobs
        and workflow.payload.get("intent") == "pause",
        "now": utcnow(),
    }
