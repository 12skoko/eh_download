import copy
import tomllib
from pathlib import Path

from sqlalchemy import func, select

from ....config import load_config
from ....db.models import MangaRecord, SpecialWorkflow
from ....db.repository import ArchiveRepository, utcnow
from ....domain.errors import ArchiveError, ErrorClass
from ....domain.models import MangaInfo
from ....integrations.http import RoleSession
from ....services.downloader.torrent import QBITTORRENT_CATEGORY, parse_torrent_options
from ....services.downloader.torrent.core import _join_external_path, _parse_size
from ....services.downloader.torrent.review import (
    WARNING_LABELS,
    candidate_snapshot,
    download_torrent,
    torrent_info_hash,
)
from ....services.paths import external_path_key, safe_filename
from ...core.contracts import OperationDefinition, WorkflowDefinition
from ...core.execution import ExecutionContext
from ...core.repository import aware
from ...core.service import SpecialConflict, SpecialInvalidRequest
from ...handlers import ModuleCapability
from ...integrations.manga import MangaIntegration, active_for_manga, bind, binding

KIND = "manual_torrent"
PHASES = {
    "awaiting_load": "等待手动加载种子",
    "loading": "正在加载候选",
    "awaiting_selection": "等待选择种子",
    "submit_queued": "等待提交",
    "submitting": "正在提交种子",
    "failed": "处理失败",
    "completed": "已交回普通下载",
    "cancelled": "已取消",
    "verify_queued": "等待核实提交",
    "verifying": "正在核实提交结果",
}


def capability(config_dir):
    path = Path(config_dir) / "special" / "manual_torrent.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw.pop("config_version", None)
    raw = raw.get(KIND, raw)
    if set(raw) - {"enabled", "max_concurrency"}:
        raise ValueError("手动种子模块配置包含未知字段")
    enabled, concurrency = raw.get("enabled", True), raw.get("max_concurrency", 1)
    if type(enabled) is not bool or type(concurrency) is not int or concurrency < 1:
        raise ValueError("手动种子模块配置无效")
    return ModuleCapability(KIND, enabled, concurrency, None if enabled else "模块已禁用")


def touch(row):
    row.row_version += 1
    row.updated_at = utcnow()


def clear_error(row):
    row.last_error_operation = row.last_error_code = row.last_error_detail = row.last_error_at = (
        None
    )


def idle(service, workflow):
    if workflow.kind != KIND or workflow.status != "active":
        raise SpecialInvalidRequest("工作流已经结束或模块不匹配")
    if any(j.status in {"queued", "running"} for j in workflow.jobs):
        raise SpecialConflict("已有任务正在排队或执行，请等待完成")


def create(service, inputs):
    if set(inputs) - {"manga_id", "row_version", "from_queue"}:
        raise SpecialInvalidRequest("未知输入字段")
    row = service.session.get(
        MangaRecord, inputs.get("manga_id"), with_for_update=True, populate_existing=True
    )
    if row is None or row.row_version != inputs.get("row_version"):
        raise SpecialConflict("档案不存在或页面已变化，请刷新")
    if inputs.get("from_queue") is True:
        if row.status == "special_processing":
            raise SpecialConflict("档案已由特殊模块接管")
    elif row.status != "manual_review" or row.last_error_operation != "torrent_download":
        raise SpecialInvalidRequest("入口要求人工复核且失败操作为 torrent_download")
    if (
        row.active_attempt_id
        or row.lease_owner
        or row.lease_token
        or row.lease_until
        or row.external_download_id
        or active_for_manga(service.session, row.manga_id)
    ):
        raise SpecialConflict("档案仍有任务、租约、外部下载或活动工作流")
    entry = {
        name: getattr(row, name)
        for name in ("last_error_operation", "last_error_code", "last_error_detail")
    }
    entry["last_error_at"] = row.last_error_at.isoformat() if row.last_error_at else None
    workflow = service.repository.create(
        KIND,
        actor=service.actor,
        resources=[f"manga:{row.manga_id}"],
        payload={"choices": [], "selection": None},
        initialize=lambda w: bind(service.session, w, row, entry=entry, resume_status=row.status),
    )
    row.status = "special_processing"
    row.status_updated_at = utcnow()
    touch(row)
    # Deliberately create no job. Only the explicit load action may access EH.
    return workflow


def queue(service, workflow, operation):
    service.enabled(KIND)
    workflow.row_version += 1
    return service.repository.queue_job(
        workflow,
        operation,
        trigger_source=service.trigger_source,
        requested_by=service.actor,
    )[0]


def load(service, workflow, inputs):
    idle(service, workflow)
    if workflow.payload.get("submission"):
        raise SpecialConflict("提交结果尚未核实，请先重试提交")
    workflow.payload = {"choices": [], "selection": None}
    workflow.phase = "awaiting_load"
    return queue(service, workflow, "load")


def choose(service, workflow, inputs):
    idle(service, workflow)
    if workflow.payload.get("submission"):
        raise SpecialConflict("已有未核实的提交，不能更换种子")
    choice = next(
        (
            c
            for c in workflow.payload.get("choices", [])
            if c["choice_id"] == inputs.get("choice_id")
        ),
        None,
    )
    if choice is None:
        raise SpecialInvalidRequest("请选择一个已加载的种子")
    # Explicit manual selection replaces the automatic flow's warning review.
    warnings = list(choice["warnings"])
    workflow.payload = {
        **workflow.payload,
        "selection": {
            "choice_id": choice["choice_id"],
            "accepted_warnings": warnings,
            "allow_personalized": inputs.get("allow_personalized") is True,
        },
    }
    workflow.phase = "submit_queued"
    service.repository._event(
        workflow,
        "manual_torrent_selected",
        operation="submit",
        actor=service.actor,
        detail=workflow.payload["selection"],
    )
    return queue(service, workflow, "submit")


def retry(service, workflow, inputs):
    idle(service, workflow)
    if not workflow.payload.get("selection"):
        return load(service, workflow, inputs)
    workflow.phase = "submit_queued"
    return queue(service, workflow, "submit")


def verify(service, workflow, inputs):
    idle(service, workflow)
    if not workflow.payload.get("submission"):
        raise SpecialInvalidRequest("没有待核实的提交")
    workflow.phase = "verify_queued"
    return queue(service, workflow, "verify")


def release_expired(service, workflow, inputs):
    if workflow.kind != KIND or workflow.status != "active":
        raise SpecialInvalidRequest("工作流已结束")
    if inputs.get("confirmed") is not True or not str(inputs.get("reason", "")).strip():
        raise SpecialInvalidRequest("请确认旧进程已经停止并填写原因")
    job = next((j for j in workflow.jobs if j.status == "running"), None)
    if not job or not job.lease_until or aware(job.lease_until) >= utcnow():
        raise SpecialConflict("没有已过期的任务")
    # Persisted submission intent is retained. All exit/reload paths refuse it;
    # the next explicit verify/retry must query qBittorrent before proceeding.
    if job.external_effect_started_at and not workflow.payload.get("submission"):
        raise SpecialConflict("缺少提交凭据，不能解除存在外部副作用的任务")
    service.repository.release_resources(workflow, job=job)
    job.status, job.finished_at = "abandoned", utcnow()
    job.lease_token = job.lease_owner = job.lease_until = None
    workflow.phase = "failed"
    workflow.row_version += 1
    service.repository._event(
        workflow,
        "special_lease_released",
        operation=job.operation,
        actor=service.actor,
        detail={"job_id": job.id, "reason": inputs["reason"]},
    )
    return workflow


def exit_workflow(service, workflow, inputs, *, direct=False):
    idle(service, workflow)
    if workflow.payload.get("submission"):
        raise SpecialConflict("可能已提交 qBittorrent，请先重试核实，不能直接退出")
    b = binding(workflow)
    row = DEFINITION.integration.records(service.session, workflow)[0]
    row.status = "download_pending" if direct else b.resume_status
    if direct:
        row.download_method, row.queue_source = "direct", "manual"
        row.next_retry_at = row.defer_until = None
        clear_error(row)
    else:
        for name, value in b.context["entry"].items():
            if name == "last_error_at" and value:
                from datetime import datetime

                value = datetime.fromisoformat(value)
            setattr(row, name, value)
    row.status_updated_at = utcnow()
    touch(row)
    workflow.status = workflow.phase = "cancelled"
    workflow.completed_at = utcnow()
    workflow.row_version += 1
    service.repository.release_resources(workflow, all_scopes=True)
    service.repository._event(
        workflow,
        "manual_torrent_exit",
        operation=None,
        actor=service.actor,
        to_status=row.status,
        detail={"direct": direct},
    )
    return workflow


class ManualIntegration(MangaIntegration):
    def validate(self, session, workflow, job):
        rows = self.records(session, workflow)
        return (
            len(rows) == 1
            and rows[0].status == "special_processing"
            and not any(
                (
                    rows[0].active_attempt_id,
                    rows[0].lease_owner,
                    rows[0].lease_token,
                    rows[0].lease_until,
                    rows[0].external_download_id,
                )
            )
        )

    def changed(self, repository, workflow, job, event):
        row = self.records(repository.session, workflow)[0]
        if event == "succeeded" and workflow.status == "completed":
            result = workflow.payload["result"]
            row.status = "downloading" if result.get("hash") else "download_pending"
            row.download_method = "torrent" if result.get("hash") else "direct"
            row.external_download_id = result.get("hash")
            row.next_retry_at = row.defer_until = None
            row.queue_source = "manual"
            row.status_updated_at = utcnow()
            clear_error(row)
            touch(row)


DEFINITION = WorkflowDefinition(
    KIND,
    "手动种子下载",
    "awaiting_load",
    {
        "load": OperationDefinition("load", frozenset({"awaiting_load"}), "loading"),
        "submit": OperationDefinition(
            "submit", frozenset({"submit_queued"}), "submitting", effect="verify"
        ),
        "verify": OperationDefinition("verify", frozenset({"verify_queued"}), "verifying"),
    },
    integration=ManualIntegration(),
    create=create,
    actions={
        "load": load,
        "choose": choose,
        "retry": retry,
        "verify": verify,
        "release-expired": release_expired,
        "cancel": exit_workflow,
        "direct": lambda s, w, i: exit_workflow(s, w, i, direct=True),
    },
)


def dashboard(session, *, page=1):
    query = select(SpecialWorkflow).where(SpecialWorkflow.kind == KIND)
    workflows = list(
        session.scalars(
            query.order_by(SpecialWorkflow.id.desc()).offset((max(1, page) - 1) * 50).limit(50)
        )
    )
    for w in workflows:
        binding(w)
    return {
        "workflows": workflows,
        "phase_labels": PHASES,
        "page": max(1, page),
        "total": session.scalar(
            select(func.count()).select_from(SpecialWorkflow).where(SpecialWorkflow.kind == KIND)
        ),
    }


def detail(session, workflow_id):
    workflow = session.get(SpecialWorkflow, workflow_id)
    return {
        "phase_labels": PHASES,
        "warning_labels": WARNING_LABELS,
        "manga_id": binding(workflow).manga_id,
        "busy": any(j.status in {"queued", "running"} for j in workflow.jobs),
    }


class ManualTorrentExecutor:
    def __init__(self, database, *, config_dir, claim, http=None, qbit=None):
        self.app, self.supervisor, self.crawl, self.secrets = load_config(config_dir)
        self.context = ExecutionContext(database, claim, config_dir=config_dir, app_config=self.app)
        self.claim = claim
        self.http = http or RoleSession(self.app, self.secrets)
        self._qbit = qbit

    @property
    def qbit(self):
        if self._qbit is None:
            from ....integrations.qbittorrent import QBittorrentClient

            options = dict(self.secrets.qbittorrent)
            options.setdefault("host", self.app.qbittorrent_url)
            self._qbit = QBittorrentClient(**options)
        return self._qbit

    def finish(self, payload, *, phase="awaiting_selection", status=None):
        with self.context.transaction() as repo:
            if not repo.succeed(self.claim, phase=phase, status=status, payload=payload):
                raise RuntimeError("stale manual torrent result")

    def run(self):
        with self.context.transaction() as repo:
            _, workflow = repo._values(self.claim)
            manga = repo.session.get(MangaRecord, binding(workflow).manga_id)
            manga_id, link, torrent_link = manga.manga_id, manga.link, manga.torrent_link
            estimated = manga.info.estimated_size_raw if manga.info else None
            complete_info = (
                manga.info is not None
                and MangaInfo(
                    manga_id=manga_id,
                    **{
                        name: getattr(manga.info, name)
                        for name in (
                            "name",
                            "link",
                            "category",
                            "uploader",
                            "language",
                            "estimated_size_raw",
                            "posted_at",
                            "pages",
                            "tags_raw",
                        )
                    },
                ).is_complete()
            )
            payload = copy.deepcopy(workflow.payload)
        if self.claim.operation in {"submit", "verify"} and payload.get("submission"):
            found = self.existing(payload["submission"])
            if found:
                payload["result"] = {"hash": found}
                return self.finish(payload, phase="completed", status="completed")
            if self.claim.operation == "verify":
                payload.pop("submission")
                payload["selection"] = None
                payload["choices"] = []
                payload["message"] = "qBittorrent 中未找到该任务；可重新加载候选、转直接下载或取消"
                return self.finish(payload, phase="awaiting_load")
        if not complete_info:
            from bs4 import BeautifulSoup

            from ....services.collector.parser import EhTagTranslation, parse_info

            html = self.http.get_text(
                link, role="browse", timeout=self.supervisor.request_timeout_seconds
            )
            info, _, _ = parse_info(BeautifulSoup(html, "lxml"), EhTagTranslation())
            info.manga_id, info.link = manga_id, link
            if not info.is_complete():
                raise ArchiveError(
                    "incomplete_details", "画廊信息不完整，请重试加载", ErrorClass.ITEM
                )
            estimated = info.estimated_size_raw
            with self.context.transaction() as repo:
                ArchiveRepository(repo.session).upsert_info(info)
        expected = _parse_size(estimated, field="estimated")
        if not torrent_link:
            raise ArchiveError("no_torrent", "没有种子页面，可转直接下载", ErrorClass.ITEM)
        response = self.http.get(
            torrent_link, role="browse", timeout=self.supervisor.request_timeout_seconds
        )
        response.raise_for_status()
        options = parse_torrent_options(
            response.text,
            include_outdated=True,
            bind_download_url=True,
            excluded_resolutions=self.crawl.excluded_resolutions,
            video_markers=self.crawl.video_markers,
        )
        payload["choices"] = [candidate_snapshot(c, expected) for c in options]
        payload["loaded_at"] = utcnow().isoformat()
        if self.claim.operation == "load":
            payload["selection"] = None
            return self.finish(payload)
        payload.pop("message", None)
        selection = payload["selection"]
        choice = next((c for c in options if c.choice_id == selection["choice_id"]), None)
        if not choice:
            if payload.get("submission"):
                raise ArchiveError(
                    "torrent_selection_stale", "候选已变化，请先核实上次提交结果", ErrorClass.ITEM
                )
            payload["selection"] = None
            payload["message"] = "候选已变化，请重新选择"
            return self.finish(payload)
        content = download_torrent(
            self.http,
            choice,
            decision=selection,
            manual_fallback=selection.get("allow_personalized") is True,
            request_options={"role": "browse", "timeout": 30},
        )
        torrent_hash = torrent_info_hash(content)
        numeric = safe_filename(manga_id.split("/", 1)[0])
        path = _join_external_path(
            self.app.qbit_torrent_path or str(self.app.root("torrent_download").resolve()), numeric
        )
        intent = {"hash": torrent_hash, "save_path": path}
        if payload.get("submission") and payload["submission"] != intent:
            raise ArchiveError(
                "torrent_selection_stale", "种子内容已变化，需核实上次提交", ErrorClass.ITEM
            )
        existing = self.existing(intent)
        if not existing:
            collision = self.qbit.find_owned(category=QBITTORRENT_CATEGORY, save_path=path)
            if collision is not None:
                raise ArchiveError(
                    "torrent_path_conflict", "下载目录已被其他种子占用", ErrorClass.ITEM
                )
            payload["submission"] = intent
            with self.context.transaction() as repo:
                repo.update_state(self.claim, payload=payload)
                if not repo.begin_external_effect(self.claim):
                    raise RuntimeError("stale manual torrent submission")
            submitted = self.qbit.add(
                content,
                save_path=path,
                display_name=numeric,
                upload_limit_bytes_per_second=self.app.torrent_upload_limit_bytes_per_second,
            )
            if submitted.lower() != torrent_hash:
                raise ArchiveError(
                    "torrent_hash_mismatch", "提交返回的种子标识不符，请重试核实", ErrorClass.ITEM
                )
        payload["result"] = {"hash": torrent_hash}
        self.finish(payload, phase="completed", status="completed")

    def existing(self, intent):
        info = self.qbit.info(intent["hash"])
        if info is None:
            return None
        if getattr(info, "category", "") != QBITTORRENT_CATEGORY or external_path_key(
            getattr(info, "save_path", "")
        ) != external_path_key(intent["save_path"]):
            raise ArchiveError(
                "torrent_ownership_conflict", "同一种子已存在于其他下载目录或分类", ErrorClass.ITEM
            )
        return intent["hash"]


def executor(database, config_dir, claim):
    return ManualTorrentExecutor(database, config_dir=config_dir, claim=claim)
