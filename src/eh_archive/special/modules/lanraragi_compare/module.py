import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select

from ....config import load_config
from ....db.models import MangaRecord, SpecialWorkflow
from ....db.repository import utcnow
from ....services.uploader.lanraragi import LANraragiApiGateway
from ...core.contracts import Integration, OperationDefinition, WorkflowDefinition
from ...core.outputs import publish_json, register_output
from ...core.repository import SpecialCancellationRequested, SpecialRepository
from .comparison import build_comparison, numeric_database_id
from .config import load_compare_config

KIND = "lanraragi_compare"

REPORT_LABELS = {
    "database_only": "仅数据库已完成记录中存在",
    "lanraragi_only": "仅 LANraragi 中存在",
    "database_duplicate_ids": "数据库重复 ID",
    "lanraragi_duplicate_ids": "LANraragi 重复 ID",
    "invalid_database_manga_ids": "数据库无效 ID",
    "unparsed_lanraragi_archives": "无法解析来源的档案",
    "lanraragi_only_database_states": "LANraragi 独有项的数据库状态",
}
PHASE_LABELS = {
    "queued": "等待执行",
    "reading_lanraragi": "读取 LANraragi",
    "reading_database": "读取数据库",
    "comparing": "比较数据",
    "publishing_report": "生成报告",
    "completed": "核对完成",
    "failed": "核对失败",
    "cancelled": "已取消",
}


def detail(session, workflow_id):
    workflow = session.get(SpecialWorkflow, workflow_id)
    summary = (workflow.payload or {}).get("summary", {})
    metrics = {key: summary[key] for key in (
        "database_only", "lanraragi_only", "database_unique_ids",
        "lanraragi_unique_ids", "unparsed_lanraragi_archives",
    ) if key in summary}
    for prefix in ("database", "lanraragi"):
        resolved = summary.get(f"{prefix}_resolved_ids", 0)
        unique = summary.get(f"{prefix}_unique_ids", 0)
        if resolved > unique:
            metrics[f"{prefix}_duplicates"] = resolved - unique
    invalid = summary.get("database_completed_rows", 0) - summary.get("database_resolved_ids", 0)
    if invalid:
        metrics["invalid_database_ids"] = invalid
    return {
        "display_summary": metrics,
        "report_presenter": present_report,
        "report_sections": report_sections,
        "summary_labels": {
            "database_completed_rows": "数据库已完成档案",
            "database_resolved_ids": "数据库有效 ID",
            "database_unique_ids": "数据库已完成 ID",
            "lanraragi_archives": "LANraragi 档案",
            "lanraragi_resolved_ids": "LANraragi 有效 ID",
            "lanraragi_unique_ids": "LANraragi ID",
            "database_duplicates": "数据库重复记录（去重减少）",
            "lanraragi_duplicates": "LANraragi 重复档案（去重减少）",
            "invalid_database_ids": "数据库无法解析 ID",
            "database_only": "数据库独有",
            "lanraragi_only": "LANraragi 独有",
            "unparsed_lanraragi_archives": "来源无法解析",
        },
        "report_labels": REPORT_LABELS,
        "phase_labels": PHASE_LABELS,
        "rerun_label": "重新核对（保留历史）",
    }


def report_sections(sections, section):
    sections = dict(sections)
    snapshots = sections.pop("lanraragi_only_database_states", [])
    by_id = {}
    for snapshot in snapshots:
        value = snapshot.get("id", snapshot.get("gid"))
        by_id.setdefault(str(value), []).append(snapshot)
    sections["lanraragi_only"] = [
        {"id": value, "snapshots": by_id.get(str(value), [])}
        for value in sections.get("lanraragi_only", [])
    ]
    if section == "lanraragi_only_database_states":
        section = "lanraragi_only"
    for key in ("database_duplicate_ids", "lanraragi_duplicate_ids"):
        if not sections.get(key):
            sections.pop(key, None)
            if section == key:
                section = "lanraragi_only"
    return sections, section


def present_report(session, section, rows):
    """Enrich only the current page; old reports remain immutable."""
    ids = set()
    for row in rows:
        value = row.get("id", row.get("gid")) if isinstance(row, dict) else row
        if str(value).isdigit():
            ids.add(str(value))
    matches = {}
    if ids:
        query = select(MangaRecord).where(or_(
            MangaRecord.manga_id.in_(ids),
            *(MangaRecord.manga_id.like(f"{value}/%") for value in sorted(ids)),
        )).order_by(MangaRecord.manga_id)
        for manga in session.scalars(query):
            matches.setdefault(str(numeric_database_id(manga.manga_id)), []).append({
                "manga_id": manga.manga_id, "name": manga.name or manga.real_name or manga.manga_id,
                "status": manga.status,
            })
    result = []
    for row in rows:
        item = dict(row) if isinstance(row, dict) else {"id": row}
        if "gid" in item:
            item["id"] = item.pop("gid")
        item["matches"] = matches.get(str(item.get("id")), [])
        current = {m["manga_id"]: m["status"] for m in item["matches"]}
        item["state_changes"] = [
            snapshot for snapshot in item.pop("snapshots", [])
            if current.get(snapshot.get("manga_id")) != snapshot.get("status")
        ]
        result.append(item)
    return result


def instance_key(url):
    parts = urlsplit(url)
    # Deliberately excludes username, password, query and fragment.
    identity = f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{parts.port or (443 if parts.scheme == 'https' else 80)}{parts.path.rstrip('/')}"
    return "lanraragi_compare:" + hashlib.sha256(identity.encode()).hexdigest()


def create(service, inputs):
    if inputs:
        raise ValueError("核对模块当前不接受自定义输入")
    key = instance_key(service.app_config.lanraragi_url)
    workflow = service.repository.create(
        KIND,
        actor=service.actor,
        payload={"input": {"database_status": "completed"}, "instance_key": key},
        resources=[key],
    )
    service.repository.queue_job(
        workflow, "compare", trigger_source=service.trigger_source, requested_by=service.actor
    )
    return workflow


class CompareIntegration(Integration):
    def resources(self, session, workflow, job):
        return (workflow.payload["instance_key"],)


DEFINITION = WorkflowDefinition(
    KIND,
    "LANraragi 数据库核对",
    "queued",
    {
        "compare": OperationDefinition(
            "compare", frozenset({"queued", "failed"}), "reading_lanraragi", lease_seconds=3600
        ),
    },
    integration=CompareIntegration(),
    create=create,
)


def dashboard(session, *, page=1):
    page = max(1, page)
    condition = SpecialWorkflow.kind == KIND
    total = session.scalar(select(func.count()).select_from(SpecialWorkflow).where(condition))
    rows = list(
        session.scalars(
            select(SpecialWorkflow)
            .where(condition)
            .order_by(SpecialWorkflow.id.desc())
            .offset((page - 1) * 50)
            .limit(50)
        )
    )
    return {"workflows": rows, "page": page, "total": total, "phase_labels": PHASE_LABELS}


class CompareExecutor:
    def __init__(self, database, *, config_dir, claim, gateway=None):
        self.database, self.claim = database, claim
        self.app, _, _, self.secrets = load_config(config_dir)
        self.config = load_compare_config(config_dir)
        self.gateway = gateway

    def progress(self, phase):
        with self.database.session() as session:
            repository = SpecialRepository(session)
            if not repository.update_progress(self.claim, {"message": phase}, phase=phase):
                raise RuntimeError("stale special job")
            repository.renew(self.claim, lease_seconds=3600)

    def run(self):
        if not self.config.enabled:
            raise ValueError("核对模块已禁用")
        started = utcnow()
        try:
            self.progress("reading_lanraragi")
            with self.database.session() as session:
                workflow = session.get(SpecialWorkflow, self.claim.workflow_id)
                if workflow.payload["instance_key"] != instance_key(self.app.lanraragi_url):
                    raise ValueError("LANraragi 实例配置已改变，请重新创建核对")
            gateway = self.gateway or LANraragiApiGateway(
                self.app.lanraragi_url,
                headers=self.secrets.lanraragi,
                timeout=self.config.timeout_seconds,
            )
            try:
                archives = gateway.list_archives()
            finally:
                if self.gateway is None and gateway.session is not None:
                    gateway.session.close()
            remote_at = utcnow()
            self.progress("reading_database")
            with self.database.session() as session:
                rows = list(session.execute(select(MangaRecord.manga_id, MangaRecord.status)))
            database_at = utcnow()
            self.progress("comparing")
            result = build_comparison(
                (mid for mid, status in rows if status == "completed"), archives
            )
            only = set(result["lanraragi_only"])
            result["lanraragi_only_database_states"] = [
                {"manga_id": mid, "status": status, "id": numeric_database_id(mid)}
                for mid, status in rows
                if numeric_database_id(mid) in only
            ]
            report = {
                "generated_at": utcnow().isoformat(),
                "database_status": "completed",
                "lanraragi_collected_at": remote_at.isoformat(),
                "database_collected_at": database_at.isoformat(),
                "elapsed_seconds": round((utcnow() - started).total_seconds(), 3),
                **result,
            }
            self.progress("publishing_report")
            output = publish_json(
                Path(self.app.log_dir) / "special_outputs",
                self.claim,
                "comparison",
                report,
                name="lanraragi_database_comparison.json",
            )
            with self.database.session() as session:
                repository = SpecialRepository(session)
                if not register_output(repository, self.claim, output):
                    raise RuntimeError("stale report publication")
                workflow = session.get(SpecialWorkflow, self.claim.workflow_id)
                payload = {
                    **workflow.payload,
                    "summary": result["summary"],
                    "lanraragi_collected_at": remote_at.isoformat(),
                    "database_collected_at": database_at.isoformat(),
                }
                if not repository.succeed(
                    self.claim,
                    phase="completed",
                    status="completed",
                    payload=payload,
                    progress={"message": "completed", "completed": 1, "total": 1},
                ):
                    raise RuntimeError("stale report result")
        except SpecialCancellationRequested:
            with self.database.session() as session:
                SpecialRepository(session).cancel_complete(self.claim, detail={"readonly": True})
