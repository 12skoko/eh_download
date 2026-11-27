"""Revisit the saved EH detail URLs of one fixed set of deferred galleries."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from requests.exceptions import RequestException
from sqlalchemy import func, select

from ...config import load_config
from ...db.models import EventLog, MangaRecord, SpecialWorkflow
from ...db.repository import ArchiveRepository, utcnow
from ...domain.errors import ArchiveError, ErrorClass, classify_exception
from ...domain.states import Status, transition_target
from ...integrations.http import RoleSession
from ...services.collector.parser import fetch_detail_page, parse_info
from ...services.collector.timing import observation_deadline
from ..core.contracts import OperationDefinition, WorkflowDefinition
from ..core.execution import ExecutionContext
from ..core.outputs import publish_json, register_output
from ..core.repository import SpecialCancellationRequested, SpecialRepository
from ..handlers import ModuleCapability

KIND = "observation_rescan"
LABELS = {
    "missing_latest": "有新版提示，但最新版尚未入库",
    "discovered": "已进入正常筛选",
    "waiting": "尚在观察期",
    "replaced": "已指向新版",
    "hidden": "已隐藏，无新版提示（人工复核）",
    "unavailable": "画廊不可用",
    "unresolved": "读取失败或需核对",
    "busy": "记录被占用，本轮跳过",
    "changed": "状态或记录已变化",
}
PHASE_LABELS = {
    "queued": "等待执行",
    "scanning": "正在补扫",
    "completed": "已完成",
    "failed": "执行中断",
    "cancelled": "已取消",
}


def _events(session, workflow_id, event_type):
    return session.scalars(
        select(EventLog.detail)
        .where(
            EventLog.run_id == f"observation:{workflow_id}",
            EventLog.event_type == event_type,
        )
        .order_by(EventLog.id)
    )


def _retry_candidates(workflow_id):
    failed = select(EventLog.detail["manga_id"].as_string()).where(
        EventLog.run_id == f"observation:{workflow_id}",
        EventLog.event_type == "rescan_result",
        EventLog.detail["outcome"].as_string() == "unresolved",
        # Older workers recorded both lock skips and deleted rows under this reason.
        EventLog.detail["reason"].as_string().is_distinct_from("档案已移除或正在由其他任务写入"),
    )
    return select(MangaRecord.manga_id).where(
        MangaRecord.status == Status.DEFERRED.value, MangaRecord.manga_id.in_(failed)
    )


def create(service, inputs):
    if set(inputs) - {"start_id", "retry_workflow_id"}:
        raise ValueError("未知的补扫参数")
    start = inputs.get("start_id")
    if start is not None and (type(start) is not int or start < 1):
        raise ValueError("起始 GID 必须是正整数")
    query = select(MangaRecord.manga_id).where(MangaRecord.status == Status.DEFERRED.value)
    retry_id = inputs.get("retry_workflow_id")
    if retry_id is not None:
        previous = service.session.get(SpecialWorkflow, retry_id) if type(retry_id) is int else None
        if not previous or previous.kind != KIND or previous.status != "completed":
            raise ValueError("请选择已完成的补扫运行")
        query = _retry_candidates(retry_id)
    workflow = service.repository.create(
        KIND, actor=service.actor, resources=[KIND], payload={"input": inputs}
    )
    candidates = []
    for mid in service.session.scalars(query):
        if start is None or 0 < numeric_gid(mid) <= start:
            candidates.append(mid)
    candidates.sort(key=lambda mid: (numeric_gid(mid), mid), reverse=True)
    # Keep the immutable list out of the frequently polled workflow payload.
    ArchiveRepository(service.session, run_id=f"observation:{workflow.id}")._event(
        None,
        "rescan_scope",
        actor=service.actor,
        operation=KIND,
        detail={"ids": candidates},
    )
    workflow.progress = {"completed": 0, "total": len(candidates)}
    service.repository.queue_job(
        workflow, "scan", requested_by=service.actor, trigger_source=service.trigger_source
    )
    return workflow


def numeric_gid(mid):
    gid = mid.partition("/")[0]
    return int(gid) if gid.isascii() and gid.isdecimal() else -1


DEFINITION = WorkflowDefinition(
    KIND,
    "观察等待补扫",
    "queued",
    {
        "scan": OperationDefinition(
            "scan", frozenset({"queued", "failed"}), "scanning", effect="idempotent"
        )
    },
    create=create,
)


def capability(config_dir):
    return ModuleCapability(KIND, True, 1)


def dashboard(session, *, page=1):
    query = select(SpecialWorkflow).where(SpecialWorkflow.kind == KIND)
    return {
        "page": page,
        "phase_labels": PHASE_LABELS,
        "total": session.scalar(select(func.count()).select_from(query.subquery())),
        "workflows": list(
            session.scalars(
                query.order_by(SpecialWorkflow.id.desc()).offset((page - 1) * 50).limit(50)
            )
        ),
    }


def detail(session, workflow_id):
    return {
        "phase_labels": PHASE_LABELS,
        "summary_labels": LABELS,
        "report_labels": LABELS,
        "report_sections": lambda sections, section: (
            sections,
            "missing_latest" if section == "database_only" else section,
        ),
    }


def live_results(session, workflow, outcome="", before=0):
    """Read committed results of this run, including work saved before a retry."""
    run = EventLog.run_id == f"observation:{workflow.id}"
    results = (run, EventLog.event_type == "rescan_result")
    category = EventLog.detail["outcome"].as_string()
    groups = session.execute(
        select(category.label("category"), func.count(), func.max(EventLog.id))
        .where(*results)
        .group_by("category")
    ).all()
    counts = {key: count for key, count, _ in groups}
    newest_id = max((last for _, _, last in groups), default=0)
    total = workflow.progress.get("total")
    if total is None:
        # Claiming or failing a job replaces its progress with a status message.
        total = session.scalar(
            select(func.json_array_length(EventLog.detail["ids"]))
            .where(run, EventLog.event_type == "rescan_scope")
        )
    outcome = outcome if outcome in LABELS else ""
    before = before if 0 < before <= newest_id else 0
    query = select(EventLog.id, EventLog.created_at, EventLog.detail).where(
        *results, EventLog.id <= newest_id,
    )
    filtered = query.where(category == outcome) if outcome else query
    page = filtered.where(EventLog.id < before) if before else filtered
    rows = session.execute(page.order_by(EventLog.id.desc()).limit(11)).all()
    latest = rows[0] if rows and not outcome and not before else None
    if newest_id and latest is None:
        latest = session.execute(query.where(EventLog.id == newest_id)).one()
    previous = None
    if before:
        # A boundary remains stable as new results arrive during polling.
        previous = session.scalar(
            filtered.with_only_columns(EventLog.id)
            .where(EventLog.id >= before)
            .order_by(EventLog.id).offset(10).limit(1)
        )
    return {
        "counts": counts, "completed": sum(counts.values()), "total": total,
        "can_retry_unresolved": (
            workflow.status == "completed" and bool(counts.get("unresolved"))
            and session.scalar(select(_retry_candidates(workflow.id).exists()))
        ),
        "latest": latest, "rows": rows[:10], "outcome": outcome, "before": before,
        "older": rows[9].id if len(rows) > 10 else None, "newer": previous or 0,
    }


def gallery_url(link, mid):
    # The browse session carries credentials. Only request the recorded EH identity.
    match = re.fullmatch(
        r"https?://(e-hentai\.org|exhentai\.org)/g/([0-9]+/[0-9a-fA-F]+)/?(?:[?#].*)?",
        link or "",
    )
    return f"https://{match[1]}/g/{mid}/" if match and match[2] == mid else None


def classify_gallery(repository, row, info, result, observation_days, *, copyright_notice=None):
    if copyright_notice is not None:
        event, outcome = "unavailable", "unavailable"
        result["reason"] = copyright_notice
        row.last_error_code = "gallery_copyright_claim"
        row.last_error_detail = copyright_notice
        row.last_error_operation, row.last_error_at = KIND, utcnow()
    elif latest := info.latest_version_id:
        version = next(v for v in info.newer_versions if v["manga_id"] == latest)
        result.update(latest_id=latest, latest_url=version["url"])
        if repository.session.get(MangaRecord, latest) is None:
            return "missing_latest"
        target = repository.resolve_discovered_replacement(row, latest)
        if target != latest:
            result.update(
                latest_id=target,
                latest_url=f"https://{urlsplit(version['url']).netloc}/g/{target}/",
            )
            if repository.session.get(MangaRecord, target) is None:
                return "missing_latest"
        row.superseded_by_id = target
        event, outcome = "outdate", "replaced"
    elif info.visible is False:
        event, outcome = "review", "hidden"
        result["reason"] = info.hidden_reason or "页面未注明隐藏原因"
        row.last_error_code = "gallery_hidden"
        row.last_error_detail = f"已隐藏，无新版提示。{result['reason']}"
        if result["reason"].casefold() == "expunged":
            event, outcome = "unavailable", "unavailable"
            row.last_error_detail = "InVisible:Expunged"
        row.last_error_operation, row.last_error_at = KIND, utcnow()
    elif info.visible is True:
        posted = row.posted_at or info.posted_at
        if posted is None:
            result["reason"] = "缺少发布时间"
            return "unresolved"
        deadline = observation_deadline(posted, observation_days)
        if utcnow() < deadline:
            result["defer_until"] = deadline.isoformat()
            return "waiting"
        event, outcome = "resume", "discovered"
    else:
        result["reason"] = "无法识别 Visible 字段"
        return "unresolved"
    row.status = transition_target(row.status, event).value
    row.defer_until = row.screen_group_id = None
    row.row_version += 1
    row.updated_at = row.status_updated_at = utcnow()
    repository._event(
        row.manga_id,
        "status_changed",
        actor=KIND,
        operation=KIND,
        from_status="deferred",
        to_status=row.status,
        detail=dict(result),
    )
    return outcome


def _scan_record(session, mid):
    """Return the record and whether a conflicting lock prevented taking it."""
    row = session.get(MangaRecord, mid, with_for_update={"skip_locked": True})
    if row is not None:
        return row, False
    # A plain MVCC read can see an occupied row without waiting for its writer.
    snapshot = session.get(MangaRecord, mid)
    return snapshot, snapshot is not None


class RescanExecutor:
    def __init__(self, database, config_dir, claim):
        app, self.supervisor, self.crawl, secrets = load_config(config_dir)
        self.context = ExecutionContext(database, claim, config_dir=config_dir, app_config=app)
        self.database, self.claim = database, claim
        self.http = RoleSession(app, secrets)

    def read(self, link):
        try:
            page = fetch_detail_page(
                self.http,
                link,
                role="browse",
                allow_redirects=False,
                check_copyright=True,
                timeout=self.supervisor.request_timeout_seconds,
            )
            if page.select_one('form input[type="password"]') and not page.select_one("#gdc"):
                raise ArchiveError(
                    "browse_login_required", "浏览账号需要重新登录", ErrorClass.SYSTEM
                )
            info, _, _ = parse_info(page)
            return info, None
        except (ArchiveError, RequestException, ValueError) as exc:
            error = classify_exception(exc)
            if error.status_code == 401 or error.code in {
                "eh_site_unavailable",
                "browse_login_required",
                "proxy_unavailable",
            }:
                raise
            return None, error

    def scan_one(self, mid, completed, total):
        with self.database.session() as session:
            before, busy = _scan_record(session, mid)
        info, error = None, None
        link = gallery_url(before.link, mid) if before else None
        if not busy and before and before.status == "deferred":
            if link:
                info, error = self.read(link)
        with self.context.transaction() as special:
            if not special.update_progress(self.claim, {"completed": completed, "total": total}):
                raise RuntimeError("stale special job")
            session = special.session
            row = None
            if before is not None and not busy:
                # The initial transaction ended before the HTTP request.
                row, busy = _scan_record(session, mid)
            repository = ArchiveRepository(session, run_id=f"observation:{self.claim.workflow_id}")
            result = {
                "manga_id": mid,
                "title": (before.name or before.real_name) if before else mid,
                "url": link,
            }
            if busy:
                outcome = "busy"
            elif row is None:
                result["reason"] = "补扫时未找到档案"
                outcome = "changed"
            elif (
                row.status != "deferred"
                or row.row_version != before.row_version
                or row.link != before.link
                or before.status != "deferred"
                or row.active_attempt_id is not None
            ):
                outcome = "changed"
            elif info is None and (error is None or error.code != "gallery_copyright_claim"):
                result["reason"] = error.code if error else "无有效的画廊详情链接"
                outcome = "unresolved"
            else:
                try:
                    outcome = classify_gallery(
                        repository, row, info, result, self.crawl.observation_days,
                        copyright_notice=error.message if error else None,
                    )
                except ArchiveError as exc:
                    if exc.info.category == ErrorClass.SYSTEM:
                        raise
                    outcome, result["reason"] = "unresolved", exc.info.message
            result["outcome"] = outcome
            # A foreign key check on an occupied gallery could itself wait for the lock.
            repository._event(
                mid if row is not None and not busy else None,
                "rescan_result", actor=KIND, operation=KIND, detail=result,
            )
            return result

    def run(self):
        try:
            self.scan()
        except SpecialCancellationRequested:
            with self.database.session() as session:
                SpecialRepository(session).cancel_complete(
                    self.claim, detail={"partial_results": True}
                )
        finally:
            self.http.session.close()

    def scan(self):
        wid = self.claim.workflow_id
        with self.context.transaction() as repository:
            ids = _events(repository.session, wid, "rescan_scope").one()["ids"]
            results = {r["manga_id"]: r for r in _events(repository.session, wid, "rescan_result")}
        self.context.progress({"completed": len(results), "total": len(ids)})
        for mid in ids:
            if mid not in results:
                results[mid] = self.scan_one(mid, len(results) + 1, len(ids))
        sections = {key: [] for key in LABELS}
        for result in results.values():
            sections[result["outcome"]].append(result)
        summary = {key: len(rows) for key, rows in sections.items()}
        output = publish_json(
            Path(self.context.app_config.log_dir) / "special_outputs",
            self.claim,
            "rescan",
            {"summary": summary, **sections},
            name="观察等待补扫.json",
        )
        with self.context.transaction() as repository:
            if not register_output(repository, self.claim, output):
                raise RuntimeError("stale special report")
            workflow = repository.session.get(SpecialWorkflow, wid)
            if not repository.succeed(
                self.claim,
                phase="completed",
                status="completed",
                payload={**workflow.payload, "summary": summary},
            ):
                raise RuntimeError("stale special job")
