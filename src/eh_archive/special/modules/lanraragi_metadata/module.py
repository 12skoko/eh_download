import json
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from ....config import load_config
from ....db.models import MangaRecord, SpecialJob, SpecialWorkflow
from ....db.repository import utcnow
from ....services.lanraragi_metadata import MetadataMaintenance, fingerprint, manga_info
from ....services.uploader.lanraragi import LANraragiApiGateway
from ...core.contracts import OperationDefinition, WorkflowDefinition
from ...core.execution import ExecutionContext
from ...core.outputs import output_path, publish_json, register_output
from ...core.repository import SpecialCancellationRequested
from ...core.service import SpecialInvalidRequest
from ...integrations.manga import active_for_manga, bind
from .config import capability, load_metadata_config
from .integration import (
    MetadataIntegration,
    eligible,
    record_success,
    reserve,
    restore,
    snapshot,
    unchanged,
)

KIND = "lanraragi_metadata"
__all__ = ["capability"]
PHASE_LABELS = {
    "queued": "等待预览",
    "scanning": "读取并比较元数据",
    "awaiting_confirmation": "等待确认",
    "applying": "更新并校验",
    "completed": "处理完成",
    "completed_with_errors": "处理结束，部分档案需复核",
    "failed": "执行失败",
    "cancelled": "已停止",
}
ACTION_LABELS = {
    "update": "需要更新",
    "verify": "内容一致，待确认",
    "updated": "已更新并校验",
    "verified": "已校验",
    "error": "需要复核",
    "cancelled": "未处理",
}


def create(service, inputs):
    config = load_metadata_config(service.config_dir)
    if set(inputs) - {"manga_ids", "mismatch_only"}:
        raise SpecialInvalidRequest("未知输入字段")
    ids = inputs.get("manga_ids", [])
    batch = inputs.get("mismatch_only", False)
    if (
        type(batch) is not bool
        or not isinstance(ids, list)
        or any(not isinstance(i, str) or not i.strip() for i in ids)
        or bool(ids) == batch
    ):
        raise SpecialInvalidRequest("请选择指定档案或元数据错误批量筛选")
    query = select(MangaRecord).order_by(MangaRecord.manga_id).with_for_update()
    if batch:
        query = query.where(
            MangaRecord.status == "manual_review",
            MangaRecord.last_error_code == "lrr_metadata_mismatch",
            MangaRecord.last_error_operation == "upload",
        ).limit(config.batch_limit + 1)
    else:
        ids = list(dict.fromkeys(i.strip() for i in ids))
        if len(ids) > config.batch_limit:
            raise SpecialInvalidRequest(f"单次最多 {config.batch_limit} 个档案")
        query = query.where(MangaRecord.manga_id.in_(ids))
    rows = list(service.session.scalars(query))
    if not rows or (not batch and len(rows) != len(ids)):
        raise SpecialInvalidRequest("没有符合条件的档案，或部分 ID 不存在")
    if len(rows) > config.batch_limit:
        raise SpecialInvalidRequest(f"超过 {config.batch_limit} 个档案，请分批指定 ID")
    for row in rows:
        if not eligible(row) or active_for_manga(service.session, row.manga_id):
            raise SpecialInvalidRequest(f"{row.manga_id} 状态不符或已有活动任务")
    workflow = service.repository.create(
        KIND,
        actor=service.actor,
        resources=[f"manga:{r.manga_id}" for r in rows],
        payload={"input": inputs, "count": len(rows)},
    )
    for row in rows:
        b = bind(service.session, workflow, row, entry={}, resume_status=row.status)
        try:
            original = snapshot(service.session, row)
            b.context = {**b.context, "snapshot": original}
        except ValueError as exc:
            b.context = {**b.context, "error": str(exc)}
    service.repository.queue_job(
        workflow, "scan", trigger_source=service.trigger_source, requested_by=service.actor
    )
    return workflow


def confirm(service, workflow, inputs):
    service.enabled(KIND)
    if inputs.get("confirmed") is not True:
        raise SpecialInvalidRequest("请先查看差异预览并确认更新")
    if workflow.phase != "awaiting_confirmation" or workflow.cancel_requested_at:
        raise SpecialInvalidRequest("当前工作流不能确认更新")
    rows = {r.manga_id: r for r in DEFINITION.integration.records(service.session, workflow)}
    ready = [b for b in workflow.manga_bindings if b.context.get("ready")]
    if not ready:
        raise SpecialInvalidRequest("没有可处理的档案")
    for b in ready:
        reserve(service.session, rows[b.manga_id], b)
    job, created = service.repository.queue_job(
        workflow, "apply", trigger_source=service.trigger_source, requested_by=service.actor
    )
    if created:
        workflow.row_version += 1
    return job


def retry(service, workflow, inputs):
    service.enabled(KIND)
    if workflow.phase != "failed" or workflow.cancel_requested_at:
        raise SpecialInvalidRequest("当前任务不能重试")
    # Every retry is a new preview, including after an uncertain remote write.
    rows = {r.manga_id: r for r in DEFINITION.integration.records(service.session, workflow)}
    for b in workflow.manga_bindings:
        if b.context.get("result"):
            continue
        row = rows[b.manga_id]
        if not eligible(row):
            b.context = {"error": "档案状态已变化，请新建预览"}
        else:
            try:
                b.context = {"snapshot": snapshot(service.session, row)}
            except ValueError as exc:
                b.context = {"error": str(exc)}
    workflow.phase = "queued"
    workflow.row_version += 1
    return service.repository.queue_job(
        workflow, "scan", trigger_source=service.trigger_source, requested_by=service.actor
    )[0]


def cancel(service, workflow, inputs):
    if workflow.status != "active":
        raise SpecialInvalidRequest("任务已结束")
    jobs = list(
        service.session.scalars(
            select(SpecialJob).where(
                SpecialJob.workflow_id == workflow.id, SpecialJob.status.in_(("queued", "running"))
            )
        )
    )
    workflow.cancel_requested_at = utcnow()
    workflow.row_version += 1
    for job in jobs:
        if job.status == "queued":
            job.status, job.finished_at = "cancelled", utcnow()
    if not any(j.status == "running" for j in jobs):
        rows = {r.manga_id: r for r in DEFINITION.integration.records(service.session, workflow)}
        for b in workflow.manga_bindings:
            restore(rows[b.manga_id], b)
        workflow.status = workflow.phase = "cancelled"
        workflow.completed_at = utcnow()
        service.repository.release_resources(workflow, all_scopes=True)
    service.repository._event(
        workflow,
        "special_cancel_requested",
        operation=None,
        actor=service.actor,
        detail={"preserve_verified_results": True},
    )
    return workflow


def require_confirmation(inputs):
    raise SpecialInvalidRequest("请使用预览页的确认更新按钮")


DEFINITION = WorkflowDefinition(
    KIND,
    "LANraragi 元数据更新",
    "queued",
    {
        "scan": OperationDefinition("scan", frozenset({"queued"}), "scanning", lease_seconds=600),
        "apply": OperationDefinition(
            "apply",
            frozenset({"awaiting_confirmation"}),
            "applying",
            lease_seconds=600,
            effect="idempotent",
            validate_input=require_confirmation,
        ),
    },
    integration=MetadataIntegration(),
    create=create,
    actions={"confirm": confirm, "retry": retry, "cancel": cancel},
)


def dashboard(session, *, page=1):
    condition = SpecialWorkflow.kind == KIND
    return {
        "workflows": list(
            session.scalars(
                select(SpecialWorkflow)
                .where(condition)
                .order_by(SpecialWorkflow.id.desc())
                .offset((max(1, page) - 1) * 50)
                .limit(50)
            )
        ),
        "total": session.scalar(select(func.count()).select_from(SpecialWorkflow).where(condition)),
        "page": max(1, page),
        "phase_labels": PHASE_LABELS,
    }


def report_sections(sections, section):
    return {"results": sections.get("results", [])}, "results"


def detail(session, workflow_id):
    workflow = session.get(SpecialWorkflow, workflow_id)
    return {
        "phase_labels": PHASE_LABELS,
        "action_labels": ACTION_LABELS,
        "active_metadata_job": session.scalar(
            select(SpecialJob.id)
            .where(
                SpecialJob.workflow_id == workflow_id, SpecialJob.status.in_(("queued", "running"))
            )
            .limit(1)
        )
        is not None,
        "report_sections": report_sections,
        "report_template": "special/lanraragi_metadata_report.html",
        "confirmed_items": [
            {"manga_id": b.manga_id, "action": b.context["result"]}
            for b in workflow.manga_bindings
            if b.context.get("result")
        ],
    }


class MetadataExecutor:
    def __init__(self, database, *, config_dir, claim, gateway=None):
        self.database, self.claim = database, claim
        self.app, _, _, secrets = load_config(config_dir)
        self.config = load_metadata_config(config_dir)
        self.context = ExecutionContext(database, claim, config_dir=config_dir, app_config=self.app)
        self.maintenance = MetadataMaintenance(
            gateway
            or LANraragiApiGateway(
                self.app.lanraragi_url,
                headers=secrets.lanraragi,
                timeout=self.config.timeout_seconds,
            )
        )
        self.connection_key = fingerprint(
            {"database": self.app.database_url, "lanraragi": self.app.lanraragi_url}
        )

    def checkpoint(self):
        with self.context.transaction() as repo:
            job, workflow = repo._values(self.claim)
            repo._check_cancel(workflow, job)

    def run(self):
        if not self.config.enabled:
            raise ValueError("元数据更新模块已禁用")
        results = []
        ids = []
        try:
            self.checkpoint()
            with self.context.transaction() as repo:
                workflow = repo.session.get(SpecialWorkflow, self.claim.workflow_id)
                ids = [b.manga_id for b in workflow.manga_bindings]
                payload = dict(workflow.payload)
            previews = {}
            if self.claim.operation == "apply":
                entry = next(e for e in payload["outputs"] if e["id"] == "preview")
                path = output_path(Path(self.app.log_dir) / "special_outputs", entry["storage_key"])
                report = json.loads(path.read_text(encoding="utf-8"))
                if report["connection_key"] != self.connection_key:
                    raise ValueError("连接配置已变化，请重新预览")
                previews = {r["manga_id"]: r for r in report["results"]}
            for index, mid in enumerate(ids):
                self.checkpoint()
                result = {"manga_id": mid, "action": "error"}
                try:
                    with self.context.transaction() as repo:
                        workflow = repo.session.get(SpecialWorkflow, self.claim.workflow_id)
                        b = next(b for b in workflow.manga_bindings if b.manga_id == mid)
                        if b.context.get("result"):
                            results.append(
                                {
                                    **result,
                                    "action": b.context["result"],
                                    "archive_id": b.context["archive_id"],
                                }
                            )
                            continue
                        if self.claim.operation == "apply" and not b.context.get("ready"):
                            results.append(previews[mid])
                            continue
                        if b.context.get("error"):
                            raise ValueError(b.context["error"])
                        row = repo.session.scalar(
                            select(MangaRecord).where(MangaRecord.manga_id == mid).with_for_update()
                        )
                        reserved = self.claim.operation == "apply"
                        if not unchanged(
                            repo.session, row, b.context["snapshot"], reserved=reserved
                        ):
                            raise ValueError("本地档案在预览后已变化，请重新预览")
                        if reserved and row.status != "special_processing":
                            raise ValueError("档案已不属于本次更新任务")
                        info = manga_info(row.info)
                        candidate = dict(b.context["snapshot"])
                    if self.claim.operation == "scan":
                        result = self.maintenance.preview(candidate, info)
                        with self.context.transaction() as repo:
                            workflow = repo.session.get(SpecialWorkflow, self.claim.workflow_id)
                            b = next(b for b in workflow.manga_bindings if b.manga_id == mid)
                            resource = (
                                f"lanraragi-metadata:{self.connection_key}:{result['archive_id']}"
                            )
                            if not repo.acquire_resources(workflow, [resource]):
                                raise ValueError("远端档案已有其他元数据更新任务")
                            b.context = {**b.context, "ready": True}
                    else:
                        with self.context.transaction() as repo:
                            if not repo.begin_external_effect(self.claim):
                                raise RuntimeError("任务租约已失效")
                        action = self.maintenance.apply(
                            previews[mid], info, checkpoint=self.checkpoint
                        )
                        result = {**previews[mid], "action": action}
                        with self.context.transaction() as repo:
                            workflow = repo.session.get(SpecialWorkflow, self.claim.workflow_id)
                            b = next(b for b in workflow.manga_bindings if b.manga_id == mid)
                            row = repo.session.scalar(
                                select(MangaRecord)
                                .where(MangaRecord.manga_id == mid)
                                .with_for_update()
                            )
                            record_success(
                                repo,
                                workflow,
                                b,
                                row,
                                result["archive_id"],
                                action,
                                self.claim.lease_owner,
                            )
                except ValueError as exc:
                    result = {"manga_id": mid, "action": "error", "detail": str(exc)}
                results.append(result)
                self.context.progress({"completed": index + 1, "total": len(ids)})
            if self.claim.operation == "scan":
                counts = Counter(r.get("archive_id") for r in results if r.get("archive_id"))
                duplicates = {key for key, count in counts.items() if count > 1}
                if duplicates:
                    with self.context.transaction() as repo:
                        workflow = repo.session.get(SpecialWorkflow, self.claim.workflow_id)
                        for result in results:
                            if result.get("archive_id") in duplicates:
                                result.update(action="error", detail="多个本地档案指向同一远端文件")
                                b = next(
                                    b
                                    for b in workflow.manga_bindings
                                    if b.manga_id == result["manga_id"]
                                )
                                b.context = {**b.context, "ready": False}
            summary = dict(Counter(r["action"] for r in results))
            waiting = self.claim.operation == "scan" and any(
                r["action"] in {"update", "verify"} for r in results
            )
            phase = (
                "awaiting_confirmation"
                if waiting
                else ("completed_with_errors" if summary.get("error") else "completed")
            )
            self.publish_result(results, summary, phase, "active" if waiting else "completed")
        except SpecialCancellationRequested:
            processed = len(results)
            done = {r["manga_id"] for r in results}
            results.extend(
                {"manga_id": mid, "action": "cancelled"} for mid in ids if mid not in done
            )
            entry = publish_json(
                Path(self.app.log_dir) / "special_outputs",
                self.claim,
                "partial",
                {"results": results, "summary": dict(Counter(r["action"] for r in results))},
                name="metadata_partial.json",
            )
            with self.context.transaction() as repo:
                # Register the internally generated cancellation report in the
                # same fenced transaction as cancellation (normal registration
                # intentionally rejects jobs with a pending cancellation).
                workflow = repo.session.get(SpecialWorkflow, self.claim.workflow_id)
                outputs = {e["id"]: e for e in workflow.payload.get("outputs", [])}
                outputs[entry["id"]] = entry
                workflow.payload = {**workflow.payload, "outputs": list(outputs.values())}
                repo.cancel_complete(self.claim, detail={"processed": processed})

    def publish_result(self, results, summary, phase, status):
        entry = publish_json(
            Path(self.app.log_dir) / "special_outputs",
            self.claim,
            "preview" if self.claim.operation == "scan" else "result",
            {
                "results": results,
                "summary": summary,
                "connection_key": self.connection_key,
                "generated_at": utcnow().isoformat(),
            },
            name=f"metadata_{self.claim.operation}.json",
        )
        with self.context.transaction() as repo:
            workflow = repo.session.get(SpecialWorkflow, self.claim.workflow_id)
            if not register_output(repo, self.claim, entry) or not repo.succeed(
                self.claim,
                phase=phase,
                status=status,
                payload={**workflow.payload, "summary": summary},
                progress={"completed": len(results), "total": len(results)},
            ):
                raise RuntimeError("元数据结果已过期")


def executor(database, config_dir, claim):
    return MetadataExecutor(database, config_dir=config_dir, claim=claim)
