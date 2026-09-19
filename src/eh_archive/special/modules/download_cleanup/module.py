import hashlib
import json
import logging
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from ....config import load_config
from ....db.models import SpecialJob, SpecialWorkflow
from ....db.repository import utcnow
from ....integrations.qbittorrent import QBittorrentClient
from ...core.contracts import Integration, OperationDefinition, WorkflowDefinition
from ...core.execution import ExecutionContext
from ...core.outputs import output_path, publish_json
from ...core.repository import SpecialCancellationRequested, SpecialRepository
from ...core.service import SpecialConflict, SpecialInvalidRequest
from .config import capability
from .preview import apply_preview, build_preview

KIND = "download_cleanup"
PHASE_LABELS = {
    "queued": "等待扫描",
    "scanning": "正在扫描",
    "awaiting_confirmation": "等待确认清理",
    "nothing_to_clean": "扫描完成，无可清理条目",
    "cleaning": "正在清理",
    "completed": "清理完成",
    "completed_with_errors": "清理结束（有失败项）",
    "failed": "执行失败",
    "cancelled": "已停止",
}
ACTION_LABELS = {
    "would_delete": "待清理",
    "deleted": "已删除",
    "already_missing": "目标已不存在",
    "delete_failed": "删除失败",
    "torrent_delete_failed": "保留种子目录",
    "target_changed": "目标已变化，跳过",
    "status_skipped": "状态不符合，跳过",
    "active_task_skipped": "有活动任务，跳过",
    "database_not_found": "数据库无记录，跳过",
    "root_missing": "下载目录不存在",
    "root_not_directory": "下载路径不是目录",
    "symlink_skipped": "链接已跳过",
    "name_not_recognized": "名称无法识别，跳过",
}
SOURCE_LABELS = {
    "qbittorrent": "qBittorrent 种子",
    "torrent_download": "种子下载目录",
    "direct_download": "直链文件",
    "hah_download": "H@H 目录",
    "aria2_download": "aria2 文件",
}


def create(service, inputs):
    if inputs:
        raise SpecialInvalidRequest("清理模块不接受输入")
    workflow = service.repository.create(KIND, actor=service.actor, payload={})
    service.repository.queue_job(
        workflow, "scan", trigger_source=service.trigger_source, requested_by=service.actor
    )
    return workflow


def confirm(service, workflow, inputs):
    service.enabled(KIND)
    if inputs.get("confirmed") is not True:
        raise SpecialInvalidRequest("请先查看预览并确认删除清单中的种子和文件")
    if workflow.cancel_requested_at or workflow.phase != "awaiting_confirmation":
        raise SpecialConflict("当前任务不能确认清理，请刷新页面")
    if not workflow.payload.get("summary", {}).get("would_delete"):
        raise SpecialInvalidRequest("没有可清理的条目")
    job, created = service.repository.queue_job(
        workflow, "apply", trigger_source=service.trigger_source, requested_by=service.actor
    )
    if created:
        workflow.row_version += 1
    return job


def retry(service, workflow, inputs):
    service.enabled(KIND)
    if workflow.phase != "failed" or any(j.operation == "apply" for j in workflow.jobs):
        raise SpecialInvalidRequest("清理执行后请创建新的扫描，重新核对残留项")
    job, created = service.repository.queue_job(
        workflow, "scan", trigger_source=service.trigger_source, requested_by=service.actor
    )
    if created:
        workflow.row_version += 1
    return job


def require_confirmation(inputs):
    raise SpecialInvalidRequest("请使用确认清理入口")


def cancel(service, workflow, inputs):
    if workflow.status != "active":
        raise SpecialConflict("任务已结束")
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
        workflow.status, workflow.phase, workflow.completed_at = "cancelled", "cancelled", utcnow()
        service.repository.release_resources(workflow, all_scopes=True)
    service.repository._event(
        workflow,
        "special_cancel_requested",
        operation=None,
        actor=service.actor,
        detail={"preserve_remaining_files": True},
    )
    return workflow


class CleanupIntegration(Integration):
    def resources(self, session, workflow, job):
        return ("download_cleanup",)


DEFINITION = WorkflowDefinition(
    KIND,
    "下载残留清理",
    "queued",
    {
        "scan": OperationDefinition(
            "scan", frozenset({"queued", "failed"}), "scanning", lease_seconds=3600
        ),
        "apply": OperationDefinition(
            "apply",
            frozenset({"awaiting_confirmation"}),
            "cleaning",
            lease_seconds=3600,
            effect="idempotent",
            validate_input=require_confirmation,
        ),
    },
    integration=CleanupIntegration(),
    create=create,
    actions={"confirm": confirm, "retry": retry, "cancel": cancel},
)


def dashboard(session, *, page=1):
    page = max(1, page)
    condition = SpecialWorkflow.kind == KIND
    return {
        "workflows": list(
            session.scalars(
                select(SpecialWorkflow)
                .where(condition)
                .order_by(SpecialWorkflow.id.desc())
                .offset((page - 1) * 50)
                .limit(50)
            )
        ),
        "total": session.scalar(select(func.count()).select_from(SpecialWorkflow).where(condition)),
        "page": page,
        "phase_labels": PHASE_LABELS,
    }


def report_sections(sections, section):
    rows = sections.get("results", [])
    groups = {"results": rows}
    for row in rows:
        groups.setdefault(row["action"], []).append(row)
    return groups, "results" if section == "database_only" else section


def detail(session, workflow_id):
    active = (
        session.scalar(
            select(SpecialJob.id).where(
                SpecialJob.workflow_id == workflow_id, SpecialJob.status.in_(("queued", "running"))
            )
        )
        is not None
    )
    applied = (
        session.scalar(
            select(SpecialJob.id).where(
                SpecialJob.workflow_id == workflow_id, SpecialJob.operation == "apply"
            )
        )
        is not None
    )
    return {
        "phase_labels": PHASE_LABELS,
        "action_labels": ACTION_LABELS,
        "source_labels": SOURCE_LABELS,
        "active_cleanup_job": active,
        "cleanup_applied": applied,
        "report_sections": report_sections,
        "report_labels": {"results": "全部条目", **ACTION_LABELS},
        "report_template": "special/download_cleanup_report.html",
    }


class CleanupExecutor:
    def __init__(self, database, *, config_dir, claim, qbit=None):
        self.database, self.claim, self.config_dir = database, claim, config_dir
        self.app, _, _, self.secrets = load_config(config_dir)
        self.qbit = qbit
        self.context = ExecutionContext(database, claim, config_dir=config_dir, app_config=self.app)

    def checkpoint(self):
        with self.context.transaction() as repository:
            values = repository._values(self.claim)
            repository._check_cancel(values[1], values[0])
            if not repository.renew(self.claim, lease_seconds=3600):
                raise RuntimeError("清理任务租约已失效")

    def run(self):
        if not capability(self.config_dir).enabled:
            raise ValueError("下载残留清理已禁用")
        partial = []
        payload = {}
        try:
            self.checkpoint()
            with self.database.session() as session:
                payload = dict(session.get(SpecialWorkflow, self.claim.workflow_id).payload)
            options = dict(self.secrets.qbittorrent)
            options.setdefault("host", self.app.qbittorrent_url)
            connection_key = hashlib.sha256(
                json.dumps(
                    {"qbit": options, "database": self.app.database_url},
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            qbit = self.qbit or QBittorrentClient(**options)
            if self.claim.operation == "scan":
                report = build_preview(
                    database=self.database,
                    app=self.app,
                    qbit=qbit,
                    checkpoint=self.checkpoint,
                )
                report["connection_key"] = connection_key
                phase = (
                    "awaiting_confirmation"
                    if report["summary"].get("would_delete")
                    else "nothing_to_clean"
                )
            elif self.claim.operation == "apply":
                entry = next(e for e in payload["outputs"] if e["id"] == "preview")
                path = output_path(Path(self.app.log_dir) / "special_outputs", entry["storage_key"])
                preview = json.loads(path.read_text(encoding="utf-8"))
                if preview.get("connection_key") != connection_key:
                    raise ValueError("数据库或 qBittorrent 连接配置已改变，请重新扫描")
                with self.context.transaction() as repository:
                    if not repository.begin_external_effect(self.claim):
                        raise RuntimeError("清理任务租约已失效")

                def collect(row):
                    partial.append(dict(row))
                    logging.getLogger(__name__).info("cleanup result: %s", row)

                report = apply_preview(
                    database=self.database,
                    app=self.app,
                    qbit=qbit,
                    preview=preview,
                    checkpoint=self.checkpoint,
                    on_result=collect,
                )
                phase = (
                    "completed_with_errors"
                    if any(
                        report["summary"].get(key)
                        for key in ("delete_failed", "torrent_delete_failed")
                    )
                    else "completed"
                )
            else:
                raise ValueError("未知清理操作")
            report["generated_at"] = utcnow().isoformat()
            self.context.publish(
                "preview" if self.claim.operation == "scan" else "cleanup",
                report,
                name=f"download_cleanup_{self.claim.operation}.json",
            )
            with self.context.transaction() as repository:
                workflow = repository.session.get(SpecialWorkflow, self.claim.workflow_id)
                if not repository.succeed(
                    self.claim,
                    phase=phase,
                    status="active" if phase == "awaiting_confirmation" else "completed",
                    payload={**workflow.payload, "summary": report["summary"]},
                    progress={"message": phase, "completed": len(report["results"])},
                ):
                    raise RuntimeError("清理结果已过期")
        except SpecialCancellationRequested:
            entry = None
            if partial:
                entry = publish_json(
                    Path(self.app.log_dir) / "special_outputs",
                    self.claim,
                    "cleanup-partial",
                    {
                        "mode": "partial",
                        "results": partial,
                        "summary": dict(Counter(r["action"] for r in partial)),
                    },
                    name="download_cleanup_partial.json",
                )
            with self.database.session() as session:
                repository = SpecialRepository(session)
                values = repository._values(self.claim)
                if values:
                    values[1].payload = {
                        **values[1].payload,
                        "partial_summary": dict(Counter(r["action"] for r in partial)),
                        "outputs": [
                            *values[1].payload.get("outputs", []),
                            *([entry] if entry else []),
                        ],
                    }
                    repository.cancel_complete(self.claim, detail={"processed": len(partial)})


def executor(database, config_dir, claim):
    return CleanupExecutor(database, config_dir=config_dir, claim=claim)
