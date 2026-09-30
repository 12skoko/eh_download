"""Optional lifecycle facilities; module policy owns all business decisions."""

from datetime import timedelta

from sqlalchemy import select

from ...db.models import SpecialJob, SpecialWorkflow, SystemControl
from ...db.repository import utcnow
from .contracts import RecoveryContext
from .registry import get_operation, get_workflow_definition
from .repository import SpecialRepository, aware


def request_cooperative_stop(session, *, owner, job_ids, reason):
    repository = SpecialRepository(session)
    repository.scheduling_lock()
    notified = set()
    for job in session.scalars(
        select(SpecialJob)
        .where(
            SpecialJob.id.in_(tuple(job_ids)),
            SpecialJob.status == "running",
            SpecialJob.lease_owner == owner,
        )
        .with_for_update()
    ):
        workflow = session.get(SpecialWorkflow, job.workflow_id)
        try:
            policy = get_workflow_definition(workflow.kind).lifecycle
        except ValueError:
            continue
        if policy is None or not policy.cooperative_stop:
            continue
        notified.add(job.id)
        if (job.progress or {}).get("_execution_control", {}).get("stop_reason"):
            continue
        job.progress = {
            **(job.progress or {}),
            "_execution_control": {
                "stop_reason": str(reason),
                "requested_at": utcnow().isoformat(),
                "owner": owner,
            },
        }
        repository._changed(workflow, job, "stop_requested")
        repository._event(
            workflow,
            "special_stop_requested",
            operation=job.operation,
            actor=owner,
            detail={"job_id": job.id, "reason": str(reason)},
        )
    return notified


def _jobs(session, workflow):
    active = list(
        session.scalars(
            select(SpecialJob)
            .where(
                SpecialJob.workflow_id == workflow.id,
                SpecialJob.status.in_(("queued", "running")),
            )
            .with_for_update()
        )
    )
    latest = session.scalar(
        select(SpecialJob)
        .where(SpecialJob.workflow_id == workflow.id)
        .order_by(SpecialJob.id.desc())
        .limit(1)
    )
    started = session.scalar(
        select(SpecialJob)
        .where(
            SpecialJob.workflow_id == workflow.id,
            SpecialJob.started_at.is_not(None),
        )
        .order_by(SpecialJob.id.desc())
        .limit(1)
    )
    rows = {job.id: job for job in (*active, latest, started) if job is not None}
    return active, latest, started, tuple(rows[key] for key in sorted(rows, reverse=True))


def _mark(repository, workflow, latest, owner):
    state = {
        **(workflow.payload or {}).get("_lifecycle", {}),
        "owner": owner,
        "settled_job": latest.id if latest else None,
        "settled_status": latest.status if latest else None,
    }
    if state != (workflow.payload or {}).get("_lifecycle", {}):
        workflow.payload = {**(workflow.payload or {}), "_lifecycle": state}
        workflow.row_version += 1
        workflow.updated_at = utcnow()


def recover_workflows(
    session,
    *,
    owner,
    enabled_kinds,
    config_dir,
    app_config,
    exited_job_ids=(),
    after_id=0,
    limit=100,
):
    """Inspect a bounded page before scheduling; None means the scan is complete.

    Expired leases never prove process death. Explicit fencing capability or a
    locally confirmed child exit is required before abandoning a running job.
    """
    repository = SpecialRepository(session, timezone=app_config.timezone)
    repository.scheduling_lock()
    control = session.get(SystemControl, "supervisor", with_for_update=True)
    now = utcnow()
    if (
        control is None
        or control.lease_owner != owner
        or not control.lease_until
        or aware(control.lease_until) <= now
    ):
        return None
    definitions = {}
    for kind in enabled_kinds:
        try:
            definition = get_workflow_definition(kind)
        except ValueError:
            continue
        if definition.lifecycle and definition.lifecycle.recover:
            definitions[kind] = definition
    if not definitions:
        return None
    limit = max(1, int(limit))
    workflows = list(
        session.scalars(
            select(SpecialWorkflow)
            .where(
                SpecialWorkflow.status == "active",
                SpecialWorkflow.kind.in_(tuple(definitions)),
                SpecialWorkflow.id > after_id,
                SpecialWorkflow.cancel_requested_at.is_(None),
            )
            .order_by(SpecialWorkflow.id)
            .limit(limit + 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    exited_job_ids = set(exited_job_ids)
    for workflow in workflows[:limit]:
        definition = definitions[workflow.kind]
        if workflow.schema_version not in definition.readable_versions:
            continue
        policy = definition.lifecycle
        active, latest, started, jobs = _jobs(session, workflow)
        state = (workflow.payload or {}).get("_lifecycle", {})
        if started is None:
            _mark(repository, workflow, latest, owner)
            continue
        running = [job for job in active if job.status == "running"]
        restarted = state.get("owner") != owner
        ended = not active and (
            state.get("settled_job") != (latest.id if latest else None)
            or state.get("settled_status") != (latest.status if latest else None)
        )
        orphaned = any(
            job.id in exited_job_ids
            or job.lease_owner != owner
            or not job.lease_until
            or aware(job.lease_until) <= now
            for job in running
        )
        if not (restarted or ended or orphaned):
            continue
        safe = True
        for job in running:
            operation = get_operation(workflow.kind, job.operation)
            if operation.effect == "verify" and job.external_effect_started_at:
                safe = False
                break
            fenced = (
                job.operation in policy.fenced_operations
                and not job.external_effect_started_at
                and (
                    job.lease_owner != owner or not job.lease_until or aware(job.lease_until) <= now
                )
            )
            if job.id not in exited_job_ids and not fenced:
                safe = False
                break
        if not safe:
            continue
        reason = (
            "restart"
            if restarted
            else (
                "worker_exit" if ended or any(j.id in exited_job_ids for j in running) else "orphan"
            )
        )
        context = RecoveryContext(reason, owner, now, config_dir, app_config)
        result = policy.recover(workflow, jobs, context)
        if result is None:
            if not running:
                _mark(repository, workflow, latest, owner)
            continue
        status = result.status or workflow.status
        if status not in {"active", "completed", "failed", "cancelled"}:
            raise ValueError("invalid recovered workflow lifecycle")
        if result.next_operation and status != "active":
            raise ValueError("a terminal recovery cannot queue another operation")
        preserved = {
            key: workflow.payload[key]
            for key in ("outputs", "_lifecycle")
            if key in (workflow.payload or {})
        }
        for job in running:
            repository.release_resources(workflow, job=job)
            job.status, job.finished_at = "abandoned", now
            job.lease_token = job.lease_owner = job.lease_until = None
            job.error_code = "execution_interrupted"
            job.error_detail = "Previous execution was stopped or fenced by lifecycle recovery"
            repository._event(
                workflow,
                "special_job_abandoned",
                operation=job.operation,
                actor=owner,
                detail={"job_id": job.id, "reason": reason},
            )
        queued = next(
            (
                job
                for job in active
                if job.status == "queued" and job.operation == result.next_operation
            ),
            None,
        )
        for job in active:
            if job.status == "queued" and job is not queued:
                repository.release_resources(workflow, job=job)
                job.status, job.finished_at = "cancelled", now
        workflow.phase, workflow.status = result.phase, status
        if result.payload is not None:
            workflow.payload = {**result.payload, **preserved}
        if result.progress is not None:
            workflow.progress = dict(result.progress)
        if status != "active":
            workflow.completed_at = now
            repository.release_resources(workflow, all_scopes=True)
        session.flush()
        if result.next_operation:
            due = now + timedelta(seconds=max(0, result.delay_seconds))
            if queued:
                queued.next_run_at = max(aware(queued.next_run_at), due)
            else:
                queued, _ = repository.queue_job(
                    workflow,
                    result.next_operation,
                    trigger_source="system",
                    requested_by=owner,
                    next_run_at=due,
                )
        _mark(repository, workflow, queued or latest, owner)
        repository._changed(workflow, queued or latest, "recovered")
        repository._event(
            workflow,
            "special_recovered",
            operation=result.next_operation,
            actor=owner,
            detail={"reason": reason, "phase": result.phase},
        )
    return workflows[limit - 1].id if len(workflows) > limit else None
