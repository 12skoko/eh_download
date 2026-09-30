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
    "positioning": "定位冻结起点",
    "pausing": "正在暂停",
    "paused": "已暂停",
    "cooling": "冷却中",
    "interrupted": "已中断",
    "waiting_repair": "等待修复",
    "completed": "本轮完成",
    "failed": "执行异常",
    "cancelled": "已取消",
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


def _new(service, scope, *, history=None, lower=None):
    payload = {
        "round_type": "backfill" if history else "history",
        "history_id": history.id if history else None,
        "scope": scope,
        "cursor": scope["initial_url"],
        "intent": "run",
        "boundary_verified": scope["mode"] == "url",
        "initialized": False,
        "upper_anchor": None,
        "lower_anchor": lower,
        "end_reached": False,
        "counts": {"pages": 0, "requests": 0, "found": 0, "created": 0, "updated": 0, "retries": 0},
        "consecutive_failures": 0,
        "request_failures": 0,
        "proxy_offset": 0,
    }
    workflow = service.repository.create(KIND, actor=service.actor, payload=payload)
    service.repository.queue_job(
        workflow,
        OPERATION,
        trigger_source=service.trigger_source,
        requested_by=service.actor,
    )
    return workflow


def create(service, inputs):
    config = _config(service)
    _assert_idle(service.session)
    scope = initial_scope(service.session, config, load_config(service.config_dir)[0], inputs)
    existing = service.session.scalars(select(SpecialWorkflow).where(SpecialWorkflow.kind == KIND))
    if any(
        w.payload.get("round_type") == "history"
        and w.payload.get("scope", {}).get("fingerprint") == scope["fingerprint"]
        for w in existing
    ):
        raise SpecialConflict("此范围已有历史轮次，请继续原轮次或手动补齐")
    return _new(service, scope)


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
    # Never resume history over an unfinished, manually created backfill.
    if workflow.payload["round_type"] == "history":
        for other in _others(service.session, workflow.id):
            if other.payload.get("history_id") == workflow.id:
                raise SpecialConflict("请先继续并完成已有补齐轮次")
    workflow.payload = {
        **workflow.payload,
        "intent": "run",
        "stop_reason": "",
        "consecutive_failures": 0,
        "request_failures": 0,
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


def coverage(session, history):
    covered = history.payload.get("upper_anchor")
    if not covered:
        raise SpecialConflict("历史起始页尚未成功落库，不能创建补齐")
    previous = history.id
    for item in session.scalars(
        select(SpecialWorkflow)
        .where(
            SpecialWorkflow.kind == KIND,
            SpecialWorkflow.status == "completed",
        )
        .order_by(SpecialWorkflow.id)
    ):
        data = item.payload
        if data.get("history_id") != history.id:
            continue
        if data["scope"]["fingerprint"] != history.payload["scope"]["fingerprint"]:
            continue
        if data.get("lower_anchor") == covered and data.get("upper_anchor"):
            covered, previous = data["upper_anchor"], item.id
    return covered, previous


def preview_backfill(service, workflow, inputs):
    config = _config(service)
    if workflow.payload.get("round_type") != "history":
        raise SpecialInvalidRequest("请在所属历史轮次上新建补齐")
    if workflow.status not in {"active", "completed"} or active_jobs(service.session, workflow.id):
        raise SpecialConflict("请先暂停历史轮次并等待安全退出")
    if workflow.status == "active" and workflow.payload.get("intent") != "pause":
        raise SpecialConflict("请先暂停历史轮次")
    _assert_idle(service.session, workflow.id)
    if any(
        w.payload.get("history_id") == workflow.id for w in _others(service.session, workflow.id)
    ):
        raise SpecialConflict("已有未完成补齐，请继续该轮")
    lower, previous = coverage(service.session, workflow)
    scope = initial_scope(
        service.session, config, load_config(service.config_dir)[0], inputs, backfill=True
    )
    if scope["fingerprint"] != workflow.payload["scope"]["fingerprint"]:
        raise SpecialConflict("账号或站点范围已改变，不能连接原覆盖边界")
    if parse_start_at(scope["upper_at"]) <= parse_start_at(lower["target_at"]):
        raise SpecialInvalidRequest("补齐新边界必须晚于已覆盖上边界")
    workflow.payload = {
        **workflow.payload,
        "backfill_preview": {
            "scope": scope,
            "lower_anchor": lower,
            "previous_workflow_id": previous,
            "created_at": utcnow().isoformat(),
        },
    }
    _changed(service, workflow, "full_collect_backfill_previewed")
    return workflow


def confirm_backfill(service, workflow, inputs):
    if inputs != {"confirmed": True}:
        raise SpecialInvalidRequest("必须先预览并确认补齐范围")
    service.enabled(KIND)
    _config(service)
    preview = workflow.payload.get("backfill_preview")
    if not preview or workflow.payload.get("round_type") != "history":
        raise SpecialConflict("没有待确认的补齐预览")
    if active_jobs(service.session, workflow.id):
        raise SpecialConflict("历史轮次尚未停止")
    if workflow.status == "active" and workflow.payload.get("intent") != "pause":
        raise SpecialConflict("历史轮次未暂停")
    _assert_idle(service.session, workflow.id)
    if any(
        w.payload.get("history_id") == workflow.id for w in _others(service.session, workflow.id)
    ):
        raise SpecialConflict("已有未完成补齐")
    if coverage(service.session, workflow) != (
        preview["lower_anchor"],
        preview["previous_workflow_id"],
    ):
        raise SpecialConflict("覆盖边界已变化，请重新预览")
    result = _new(service, preview["scope"], history=workflow, lower=preview["lower_anchor"])
    workflow.payload = {k: v for k, v in workflow.payload.items() if k != "backfill_preview"}
    _changed(service, workflow, "full_collect_backfill_created")
    return result


def reject_cancel(service, workflow, inputs):
    raise SpecialInvalidRequest("全量轮次使用 pause/resume 保留断点，不使用取消")


def recover(workflow, jobs, context):
    data = dict(workflow.payload)
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
        return job.status == "running" or workflow.payload.get("intent") == "run"


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
    integration=FullCollectIntegration(),
    create=create,
    actions={
        "pause": pause,
        "resume": resume,
        "retry": resume,
        "cancel": reject_cancel,
        "preview_backfill": preview_backfill,
        "confirm_backfill": confirm_backfill,
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
    history = (
        workflow
        if workflow.payload["round_type"] == "history"
        else session.get(
            SpecialWorkflow,
            workflow.payload["history_id"],
        )
    )
    try:
        covered, _ = coverage(session, history)
    except SpecialConflict:
        covered = None
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
        "covered_boundary": covered,
        "history_id": history.id,
        "now": utcnow(),
    }
