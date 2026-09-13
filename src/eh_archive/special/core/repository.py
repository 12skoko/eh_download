from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, timedelta

from sqlalchemy import cast, func, select, text
from sqlalchemy.dialects.postgresql import JSONB

from ...db.models import EventLog, SpecialJob, SpecialWorkflow
from ...db.repository import utcnow
from .contracts import OperationResult
from .registry import get_operation, get_workflow_definition


@dataclass(frozen=True)
class ClaimedSpecialJob:
    job_id: int
    workflow_id: int
    kind: str
    operation: str
    lease_token: str
    lease_owner: str
    workflow_version: int


class SpecialCancellationRequested(RuntimeError):
    pass


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class SpecialRepository:
    def __init__(self, session, *, run_id=None, timezone="UTC"):
        self.session, self.run_id, self.timezone = session, run_id, timezone

    def scheduling_lock(self):
        # Shared by every command, claim and result transaction; acquired before row locks.
        if self.session.bind.dialect.name == "postgresql":
            self.session.execute(text("SELECT pg_advisory_xact_lock(697321408214)"))

    def acquire_resources(self, workflow, keys, *, job=None):
        self.scheduling_lock()
        keys = sorted(set(keys))
        if any(not isinstance(k, str) or not k for k in keys):
            raise ValueError("resource keys must be nonempty strings")
        self.session.flush()
        for key in keys:
            query = select(SpecialWorkflow).where(SpecialWorkflow.id != workflow.id)
            if self.session.bind.dialect.name == "postgresql":
                query = query.where(
                    cast(SpecialWorkflow.resource_claims, JSONB).contains([{"key": key}])
                )
            for other in self.session.scalars(query):
                if any(c["key"] == key for c in other.resource_claims or []):
                    return False
        claims = {c["key"]: c for c in workflow.resource_claims or []}
        for key in keys:
            existing = claims.get(key)
            if existing and existing["scope"] == "workflow":
                continue
            claims[key] = (
                {"key": key, "scope": "workflow"}
                if job is None
                else {"key": key, "scope": "job", "job_id": job.id, "lease_token": job.lease_token}
            )
        workflow.resource_claims = [claims[k] for k in sorted(claims)]
        return True

    def release_resources(self, workflow, *, job=None, all_scopes=False):
        self.scheduling_lock()
        workflow.resource_claims = [
            c
            for c in workflow.resource_claims or []
            if not (
                all_scopes
                or (c["scope"] == "workflow" and workflow.status in {"completed", "cancelled"})
                or (job and c.get("job_id") == job.id and c.get("lease_token") == job.lease_token)
            )
        ]

    def create(self, kind, *, actor, payload=None, resources=(), initialize=None, emit_event=True):
        self.scheduling_lock()
        definition = get_workflow_definition(kind)
        workflow = SpecialWorkflow(
            kind=kind,
            schema_version=definition.schema_version,
            status="active",
            phase=definition.initial_phase,
            payload=dict(payload or {}),
            progress={},
            resource_claims=[],
            created_by=actor,
            row_version=0,
        )
        self.session.add(workflow)
        self.session.flush()
        if initialize is not None:
            initialize(workflow)
        if not self.acquire_resources(workflow, resources):
            raise ValueError("所需资源已有正在进行的工作流，请等待完成")
        if emit_event:
            self._event(workflow, "special_start", operation=None, actor=actor)
        return workflow

    def queue_job(self, workflow, operation, *, trigger_source, requested_by, next_run_at=None):
        self.scheduling_lock()
        definition = get_workflow_definition(workflow.kind)
        op = get_operation(workflow.kind, operation)
        if workflow.schema_version not in definition.readable_versions:
            raise ValueError("工作流数据版本不兼容，不能执行")
        existing = self.session.scalar(
            select(SpecialJob).where(
                SpecialJob.workflow_id == workflow.id,
                SpecialJob.operation == operation,
                SpecialJob.status.in_(("queued", "running")),
            )
        )
        if existing:
            return existing, False
        if workflow.status not in op.allowed_statuses or workflow.phase not in op.allowed_phases:
            raise ValueError("当前工作流状态不允许此操作")
        n = self.session.scalar(
            select(func.coalesce(func.max(SpecialJob.attempt_no), 0) + 1).where(
                SpecialJob.workflow_id == workflow.id, SpecialJob.operation == operation
            )
        )
        job = SpecialJob(
            workflow_id=workflow.id,
            operation=operation,
            status="queued",
            attempt_no=n,
            trigger_source=trigger_source,
            requested_by=requested_by,
            next_run_at=next_run_at or utcnow(),
            progress={},
        )
        if not definition.integration.validate(self.session, workflow, job):
            raise ValueError("关联对象状态不允许此操作")
        self.session.add(job)
        self.session.flush()
        self._event(
            workflow,
            "special_job_queued",
            operation=operation,
            actor=requested_by,
            detail={"job_id": job.id, "trigger_source": trigger_source},
        )
        return job, True

    def _queued(self, enabled_kinds):
        return (
            select(SpecialJob)
            .join(SpecialWorkflow)
            .where(
                SpecialJob.status == "queued",
                SpecialJob.next_run_at <= utcnow(),
                SpecialJob.lease_until.is_(None),
                SpecialWorkflow.kind.in_(tuple(enabled_kinds)),
            )
            .order_by(SpecialJob.next_run_at, SpecialJob.id)
        )

    def has_queued(self, *, enabled_kinds):
        return self.session.scalar(self._queued(enabled_kinds).limit(1)) is not None

    def claim_next(
        self, *, owner, lease_seconds, enabled_kinds, max_concurrency=1, module_limits=None
    ):
        self.scheduling_lock()
        running = list(
            self.session.execute(
                select(SpecialJob.workflow_id, SpecialWorkflow.kind)
                .join(SpecialWorkflow)
                .where(SpecialJob.status == "running")
            )
        )
        if len(running) >= max_concurrency:
            return None
        for job in self.session.scalars(self._queued(enabled_kinds).with_for_update(of=SpecialJob)):
            workflow = self.session.get(SpecialWorkflow, job.workflow_id)
            try:
                definition = get_workflow_definition(workflow.kind)
                op = get_operation(workflow.kind, job.operation)
            except ValueError:
                continue
            if workflow.schema_version not in definition.readable_versions:
                continue
            if any(w == workflow.id for w, _ in running):
                continue
            if sum(k == workflow.kind for _, k in running) >= (module_limits or {}).get(
                workflow.kind, max_concurrency
            ):
                continue
            if workflow.status not in op.allowed_statuses or not definition.integration.validate(
                self.session, workflow, job
            ):
                continue
            if workflow.phase not in op.allowed_phases:
                job.status, job.error_code, job.finished_at = (
                    "failed",
                    "invalid_special_phase",
                    utcnow(),
                )
                workflow.error_code = job.error_code
                workflow.error_detail = "操作与当前阶段不匹配"
                if op.affects_workflow:
                    workflow.phase = op.failure_phase
                self._changed(workflow, job, "failed")
                self._event(
                    workflow,
                    "special_job_failed",
                    operation=job.operation,
                    actor=owner,
                    error_code=job.error_code,
                    detail={"job_id": job.id},
                )
                continue
            job.lease_token = str(uuid.uuid4())
            if not self.acquire_resources(
                workflow, definition.integration.resources(self.session, workflow, job), job=job
            ):
                job.lease_token = None
                continue
            job.status, job.lease_owner = "running", owner
            job.lease_until = utcnow() + timedelta(seconds=op.lease_seconds or lease_seconds)
            job.started_at, job.error_code, job.error_detail = utcnow(), None, None
            if op.affects_workflow:
                workflow.phase = op.running_phase
            workflow.progress = {"message": op.running_phase}
            workflow.error_code = workflow.error_detail = None
            self._changed(workflow, job, "claimed")
            self._event(
                workflow,
                "special_job_claimed",
                operation=job.operation,
                actor=owner,
                detail={"job_id": job.id, "lease_owner": owner},
            )
            return ClaimedSpecialJob(
                job.id,
                workflow.id,
                workflow.kind,
                job.operation,
                job.lease_token,
                owner,
                workflow.row_version,
            )
        return None

    def validate_claim(self, job_id, *, workflow_id, lease_token, lease_owner):
        self.scheduling_lock()
        job = self.session.scalar(
            select(SpecialJob)
            .where(SpecialJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not job
            or job.workflow_id != workflow_id
            or job.status != "running"
            or job.lease_token != lease_token
            or job.lease_owner != lease_owner
            or not job.lease_until
            or aware(job.lease_until) <= utcnow()
        ):
            return None
        workflow = self.session.get(SpecialWorkflow, workflow_id)
        definition = get_workflow_definition(workflow.kind)
        op = get_operation(workflow.kind, job.operation)
        if (
            op.timeout_seconds
            and job.started_at
            and aware(job.started_at) + timedelta(seconds=op.timeout_seconds) <= utcnow()
        ):
            return None
        if (
            workflow.status not in op.allowed_statuses
            or workflow.schema_version not in definition.readable_versions
            or not definition.integration.validate(self.session, workflow, job)
        ):
            return None
        return job, workflow

    def _values(self, claim):
        values = self.validate_claim(
            claim.job_id,
            workflow_id=claim.workflow_id,
            lease_token=claim.lease_token,
            lease_owner=claim.lease_owner,
        )
        if values and (values[1].kind != claim.kind or values[0].operation != claim.operation):
            return None
        return values

    def renew(self, claim, *, lease_seconds):
        values = self._values(claim)
        if values:
            values[0].lease_until = utcnow() + timedelta(seconds=lease_seconds)
        return values is not None

    def begin_external_effect(self, claim):
        values = self._values(claim)
        if values:
            self._check_cancel(values[1], values[0])
            values[0].external_effect_started_at = utcnow()
        return values is not None

    def _check_cancel(self, workflow, job):
        if (
            workflow.cancel_requested_at
            and not get_operation(workflow.kind, job.operation).cancellation
        ):
            raise SpecialCancellationRequested()

    def _changed(self, workflow, job, event):
        workflow.row_version += 1
        workflow.updated_at = utcnow()
        get_workflow_definition(workflow.kind).integration.changed(self, workflow, job, event)

    def update_state(
        self, claim, *, payload=None, progress=None, phase=None, event_type=None, detail=None
    ):
        values = self._values(claim)
        if not values:
            return False
        job, workflow = values
        self._check_cancel(workflow, job)
        if payload is not None:
            workflow.payload = {
                **payload,
                **(
                    {"outputs": workflow.payload["outputs"]}
                    if "outputs" in (workflow.payload or {})
                    else {}
                ),
            }
        if progress is not None:
            job.progress = workflow.progress = dict(progress)
        if phase is not None and get_operation(workflow.kind, job.operation).affects_workflow:
            workflow.phase = phase
        self._changed(workflow, job, "updated")
        if event_type:
            self._event(
                workflow,
                event_type,
                operation=job.operation,
                actor=claim.lease_owner,
                detail={"job_id": job.id, **(detail or {})},
            )
        return True

    def update_progress(self, claim, progress, *, phase=None):
        return self.update_state(claim, progress=progress, phase=phase)

    def succeed(self, claim, *, phase, payload=None, progress=None, detail=None, status=None):
        if not self.update_state(claim, payload=payload, progress=progress, phase=phase):
            return False
        job, workflow = self._values(claim)
        job.status, job.finished_at, job.lease_until = "succeeded", utcnow(), None
        if status is not None:
            if (
                not get_operation(workflow.kind, job.operation).affects_workflow
                and status != workflow.status
            ):
                raise ValueError("maintenance operation cannot change workflow lifecycle")
            if status not in {"active", "completed", "failed", "cancelled"}:
                raise ValueError("invalid workflow lifecycle")
            workflow.status = status
            if status != "active":
                workflow.completed_at = utcnow()
        workflow.error_code = workflow.error_detail = None
        self._changed(workflow, job, "succeeded")
        self.release_resources(workflow, job=job)
        self._event(
            workflow,
            "special_job_succeeded",
            operation=job.operation,
            actor=claim.lease_owner,
            detail={"job_id": job.id, "phase": phase, **(detail or {})},
        )
        return True

    def commit_result(self, claim, result: OperationResult):
        if not self.succeed(
            claim,
            phase=result.phase,
            payload=result.payload,
            progress=result.progress,
            status=result.status,
        ):
            return False
        if result.next_operation:
            workflow = self.session.get(SpecialWorkflow, claim.workflow_id)
            self.queue_job(
                workflow,
                result.next_operation,
                trigger_source="system",
                requested_by=claim.lease_owner,
                next_run_at=utcnow() + timedelta(seconds=result.delay_seconds),
            )
        return True

    def fail(self, claim, *, error_code, error_detail, phase=None, payload=None):
        values = self._values(claim)
        if not values:
            return False
        job, workflow = values
        op = get_operation(workflow.kind, job.operation)
        definition = get_workflow_definition(workflow.kind)
        job.status, job.finished_at, job.lease_until = "failed", utcnow(), None
        job.error_code, job.error_detail = error_code, error_detail[:4000]
        workflow.error_code, workflow.error_detail = job.error_code, job.error_detail
        if op.affects_workflow:
            workflow.phase = phase or definition.failure_phases.get(error_code, op.failure_phase)
        if payload is not None:
            workflow.payload = dict(payload)
        workflow.progress = {"message": "failed"}
        self._changed(workflow, job, "failed")
        self.release_resources(workflow, job=job)
        self._event(
            workflow,
            "special_job_failed",
            operation=job.operation,
            actor=claim.lease_owner,
            error_code=error_code,
            detail={"job_id": job.id, "summary": error_detail[:500]},
        )
        if (
            error_code in op.retryable_errors
            and job.attempt_no < op.max_attempts
            and not workflow.cancel_requested_at
            and (op.effect != "verify" or not job.external_effect_started_at)
        ):
            self.session.flush()
            self.queue_job(
                workflow,
                job.operation,
                trigger_source="system",
                requested_by=claim.lease_owner,
                next_run_at=utcnow()
                + timedelta(seconds=op.retry_delay_seconds * 2 ** (job.attempt_no - 1)),
            )
        return True

    def cancel_complete(self, claim, *, detail):
        values = self._values(claim)
        if not values:
            return False
        job, workflow = values
        job.status = (
            "succeeded" if get_operation(workflow.kind, job.operation).cancellation else "cancelled"
        )
        job.finished_at, job.lease_until = utcnow(), None
        workflow.status, workflow.phase, workflow.completed_at = "cancelled", "cancelled", utcnow()
        workflow.error_code = workflow.error_detail = None
        self._changed(workflow, job, "cancelled")
        self.release_resources(workflow, job=job)
        self._event(
            workflow,
            "special_cancelled",
            operation=job.operation,
            actor=claim.lease_owner,
            detail={"job_id": job.id, **detail},
        )
        return True

    def _event(
        self,
        workflow,
        event_type,
        *,
        operation,
        actor,
        from_status=None,
        to_status=None,
        error_code=None,
        detail=None,
    ):
        event_detail = {**(detail or {}), "workflow_id": workflow.id, "module": workflow.kind}
        if "job_id" in event_detail:
            job = self.session.get(SpecialJob, event_detail["job_id"])
            if job is None or job.workflow_id != workflow.id:
                raise ValueError("event job does not belong to workflow")
            event_detail.update(job_id=job.id, attempt_no=job.attempt_no)
        self.session.add(
            EventLog(
                manga_id=get_workflow_definition(workflow.kind).integration.event_subject(workflow),
                run_id=self.run_id,
                component="special_processing",
                event_type=event_type,
                operation=operation,
                from_status=from_status,
                to_status=to_status,
                error_code=error_code,
                actor=actor,
                detail=event_detail,
            )
        )
