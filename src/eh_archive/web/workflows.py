"""Special-module catalog, dashboards, workflow detail panels, reports and actions.

Every write calls the same ModuleService / SpecialWorkflowService entry point as
the original pages; only the post-action navigation differs (the panel is
re-rendered in place with a toast instead of a full-page redirect).
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select

from ..db.models import MangaRecord, SpecialWorkflow
from ..special.core.service import ModuleService, SpecialInvalidRequest, SpecialServiceError
from ..special.service import (
    SpecialNotFound,
    SpecialWorkflowService,
    special_module_health,
    special_workflow_detail,
)
from .shared import _actor, _validated_form
from .services import Page
from .special_modules import get_special_module_page, special_module_cards
from .special_routes import report_context
from .common import PREFIX, UIContext, is_htmx, redirect, trigger_header

_VIDEO_OPS = {
    "load": "queue_load",
    "select": "select_torrents",
    "check": "queue_check",
    "cleanup-sources": "queue_source_cleanup",
    "retry": "retry",
    "cancel": "cancel",
    "release-expired": "release_expired_job",
    "exit-without-cleanup": "exit_without_cleanup",
}
_ACTION_TOASTS = {
    "load": "已排队加载 Torrent 列表",
    "select": "已提交选择，等待 Worker 提交到 qBittorrent",
    "check": "已排队检查这个档案",
    "cleanup-sources": "已排队清理源文件",
    "retry": "已重新排队失败阶段",
    "cancel": "已提交取消请求",
    "release-expired": "已解除过期租约",
    "exit-without-cleanup": "已保留资源并退出特殊处理",
    "confirm": "已确认，任务排队中",
    "pause": "已请求暂停，将在页面边界保存进度",
    "resume": "已继续此轮",
    "verify": "已排队核实上次提交结果",
    "direct": "已转为直接下载",
    "choose": "已保存选择，等待提交种子",
    "terminate": "已终止此轮，档案与检查点已保留",
}


_RUN_STATUSES = {"active": "进行中", "completed": "已完成", "failed": "失败", "cancelled": "已取消"}


def module_url(kind: str) -> str:
    return f"{PREFIX}/workflows/{kind}"


def workflow_url(kind: str, workflow_id: int) -> str:
    return f"{PREFIX}/workflows/{kind}/{workflow_id}"


def install(app, ctx: UIContext) -> None:
    database = ctx.database
    app_config = ctx.app_config
    config_dir = ctx.config_dir
    env = ctx.templates.env

    def pick(*names: str) -> str:
        return env.select_template(list(names)).name

    def module_or_404(kind: str):
        try:
            return get_special_module_page(kind)
        except ValueError:
            raise SpecialNotFound("特殊处理模块不存在") from None

    # ------------------------------------------------------------------ 目录
    @app.get(PREFIX + "/workflows", response_class=HTMLResponse)
    def catalog(request: Request, kind: str = "", status: str = "active", page: int = 1):
        modules = special_module_cards(config_dir)
        status = status if status in _RUN_STATUSES or status == "all" else "active"
        conditions = [SpecialWorkflow.kind == kind] if kind else []
        with database.session() as session:
            status_totals = dict(
                session.execute(
                    select(SpecialWorkflow.status, func.count())
                    .where(*conditions)
                    .group_by(SpecialWorkflow.status)
                ).all()
            )
            if status != "all":
                conditions.append(SpecialWorkflow.status == status)
            total = (
                session.scalar(select(func.count()).select_from(SpecialWorkflow).where(*conditions))
                or 0
            )
            page = min(max(1, page), max(1, (total + 49) // 50))
            runs = list(
                session.scalars(
                    select(SpecialWorkflow)
                    .where(*conditions)
                    .order_by(desc(SpecialWorkflow.updated_at), desc(SpecialWorkflow.id))
                    .offset((page - 1) * 50)
                    .limit(50)
                )
            )
            ids = {b.manga_id for w in runs for b in w.manga_bindings[:1]}
            names = (
                {
                    m.manga_id: m.name or m.real_name or m.manga_id
                    for m in session.scalars(
                        select(MangaRecord).where(MangaRecord.manga_id.in_(ids))
                    )
                }
                if ids
                else {}
            )
            rows = [
                {
                    "workflow": w,
                    "manga_id": w.manga_bindings[0].manga_id if w.manga_bindings else None,
                    "bindings": len(w.manga_bindings),
                    "running": any(j.status == "running" for j in w.jobs),
                    "queued": any(j.status == "queued" for j in w.jobs),
                }
                for w in runs
            ]
            totals = dict(
                session.execute(
                    select(SpecialWorkflow.kind, func.count()).group_by(SpecialWorkflow.kind)
                ).all()
            )
            active_totals = dict(
                session.execute(
                    select(SpecialWorkflow.kind, func.count())
                    .where(SpecialWorkflow.status == "active")
                    .group_by(SpecialWorkflow.kind)
                ).all()
            )
        query = urlencode(
            {key: value for key, value in (("kind", kind), ("status", status)) if value}
        )
        return ctx.page(
            request,
            "workflows.html",
            modules=modules,
            run_rows=rows,
            names=names,
            totals=totals,
            active_totals=active_totals,
            run_kind=kind,
            run_status=status,
            run_status_totals=status_totals,
            run_statuses=_RUN_STATUSES,
            run_page=Page(rows, page, 50, total),
            run_url=f"{PREFIX}/workflows" + (f"?{query}" if query else ""),
        )

    # ------------------------------------------------------------------ 模块控制台
    @app.get(PREFIX + "/workflows/{kind}", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        kind: str,
        page: int = 1,
        notice: str | None = None,
        found: int | None = None,
        queued: int | None = None,
        skipped: int | None = None,
    ):
        try:
            module = module_or_404(kind)
        except SpecialServiceError as exc:
            return ctx.service_error(request, exc)
        with database.session() as session:
            data = module.load_dashboard(session, page=max(1, page))
        if kind == "full_collect":
            from ..special.modules.full_collect.module import creation_defaults

            data.update(creation_defaults(config_dir))
        if kind == "video_archive":
            with database.session() as session:
                data.update(
                    page=max(1, page),
                    total=session.scalar(
                        select(func.count())
                        .select_from(SpecialWorkflow)
                        .where(SpecialWorkflow.kind == kind)
                    )
                    or 0,
                )
        return ctx.page(
            request,
            pick(f"special/{kind}.html", "special/_dashboard_generic.html"),
            **data,
            module=module,
            kind=kind,
            module_url=module_url(kind),
            module_health=special_module_health(kind, config_dir),
            notice=notice,
            batch_found=found,
            batch_queued=queued,
            batch_skipped=skipped,
        )

    # ------------------------------------------------------------------ 工作流详情
    def panel_values(
        workflow_id: int, jobs_page: int = 1, events_page: int = 1, tab: str = ""
    ) -> dict:
        with database.session() as session:
            detail = special_workflow_detail(
                session, workflow_id, jobs_page=max(1, jobs_page), events_page=max(1, events_page)
            )
            workflow = detail["workflow"]
            bindings = [(b.manga_id, b.resume_status) for b in workflow.manga_bindings]
            ids = [manga_id for manga_id, _ in bindings[:50]]
            records = (
                {
                    m.manga_id: m
                    for m in session.scalars(
                        select(MangaRecord).where(MangaRecord.manga_id.in_(ids))
                    )
                }
                if ids
                else {}
            )
        kind = workflow.kind
        compatible = not detail["execution_reason"]
        paging = detail["history_paging"]
        tab = tab if tab in {"jobs", "events"} else ""

        def history_query(**override) -> str:
            values = {"jobs_page": paging["jobs_page"], "events_page": paging["events_page"]}
            values.update(override)
            if values.get("tab", tab):
                values["tab"] = values.get("tab", tab)
            return urlencode(values)

        detail.update(
            kind=kind,
            module_url=module_url(kind),
            ui_jobs_page=Page(
                detail["jobs"], paging["jobs_page"], paging["jobs_limit"], paging["jobs_total"]
            ),
            ui_events_page=Page(
                detail["events"],
                paging["events_page"],
                paging["events_limit"],
                paging["events_total"],
            ),
            ui_tab=tab,
            ui_history_query=history_query,
            ui_panel_url=f"{PREFIX}/partials/workflow/{workflow_id}?{history_query()}",
            ui_page_url=workflow_url(kind, workflow_id),
            module_health=special_module_health(kind, config_dir),
            ui_bindings=[
                {"manga_id": i, "resume_status": r, "manga": records.get(i)}
                for i, r in bindings[:50]
            ],
            ui_binding_total=len(bindings),
            ui_panel=pick(f"special/_panel_{kind}.html", "special/_panel_generic.html")
            if compatible
            else "special/_panel_generic.html",
        )
        return detail

    @app.get(PREFIX + "/workflows/{kind}/{workflow_id:int}", response_class=HTMLResponse)
    def workflow_page(
        request: Request,
        kind: str,
        workflow_id: int,
        page: int = 1,
        jobs_page: int | None = None,
        events_page: int | None = None,
        tab: str = "",
    ):
        try:
            values = panel_values(
                workflow_id,
                page if jobs_page is None else jobs_page,
                page if events_page is None else events_page,
                tab,
            )
        except SpecialServiceError as exc:
            return ctx.service_error(request, exc)
        if values["kind"] != kind:
            return RedirectResponse(workflow_url(values["kind"], workflow_id), status_code=307)
        return ctx.page(request, "workflow.html", **values)

    @app.get(PREFIX + "/workflow/{workflow_id:int}", include_in_schema=False)
    def workflow_short(request: Request, workflow_id: int):
        with database.session() as session:
            workflow = session.get(SpecialWorkflow, workflow_id)
        if workflow is None:
            return ctx.error(request, "特殊工作流不存在", 404)
        target = workflow_url(workflow.kind, workflow_id)
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=307)

    @app.get(PREFIX + "/partials/workflow/{workflow_id:int}", response_class=HTMLResponse)
    def workflow_partial(
        request: Request, workflow_id: int, jobs_page: int = 1, events_page: int = 1, tab: str = ""
    ):
        try:
            values = panel_values(workflow_id, jobs_page, events_page, tab)
        except SpecialServiceError as exc:
            return ctx.service_error(request, exc)
        return ctx.partial(request, values["ui_panel"], **values)

    def after_action(request, workflow_id: int, key: str):
        if not is_htmx(request):
            with database.session() as session:
                workflow = session.get(SpecialWorkflow, workflow_id)
            return redirect(request, workflow_url(workflow.kind, workflow_id), "workflow-updated")
        values = panel_values(workflow_id)
        headers = trigger_header(toast=_ACTION_TOASTS.get(key, "操作已提交"))
        return ctx.partial(request, values["ui_panel"], headers=headers, **values)

    def created(request, workflow):
        return redirect(request, workflow_url(workflow.kind, workflow.id), "workflow-created")

    def failure(request, exc):
        error = (
            exc
            if isinstance(exc, SpecialServiceError)
            else SpecialInvalidRequest(str(exc) or "表单数据无效")
        )
        return ctx.service_error(request, error)

    def module_service(session, request):
        return ModuleService(
            session, actor=_actor(request), config_dir=config_dir, app_config=app_config
        )

    def video_service(session, request):
        return SpecialWorkflowService(
            session, actor=_actor(request), config_dir=config_dir, app_config=app_config
        )

    # ------------------------------------------------------------------ 通用动作
    @app.post(PREFIX + "/workflows/{kind}/create")
    async def create(request: Request, kind: str):
        form = await _validated_form(request)
        try:
            inputs = json.loads(str(form.get("inputs", "{}")))
            if not isinstance(inputs, dict):
                raise SpecialInvalidRequest("输入必须是对象")
            with database.session() as session:
                workflow = module_service(session, request).create(kind, inputs)
        except ValueError as exc:
            return failure(request, exc)
        return created(request, workflow)

    @app.post(PREFIX + "/workflow/{workflow_id:int}/actions/{action}")
    async def action(request: Request, workflow_id: int, action: str):
        form = await _validated_form(request)
        try:
            inputs = json.loads(str(form.get("inputs", "{}")))
            if action == "release-expired" and "reason" in form:
                inputs = {
                    "reason": str(form.get("reason", "")),
                    "confirmed": form.get("confirmed") == "yes",
                }
            if action in {"confirm", "terminate"} and "confirmed" in form:
                inputs = {"confirmed": form.get("confirmed") == "yes"}
            if not isinstance(inputs, dict):
                raise SpecialInvalidRequest("输入必须是对象")
            with database.session() as session:
                module_service(session, request).action(
                    workflow_id,
                    action,
                    row_version=int(str(form.get("row_version", ""))),
                    inputs=inputs,
                )
        except ValueError as exc:
            return failure(request, exc)
        return after_action(request, workflow_id, action)

    # ------------------------------------------------------------------ 视频模块
    @app.post(PREFIX + "/workflow/{workflow_id:int}/video/{op}")
    async def video_op(request: Request, workflow_id: int, op: str):
        form = await _validated_form(request)
        method = _VIDEO_OPS.get(op)
        if method is None:
            return ctx.error(request, "未知操作", 404)
        try:
            kwargs = {"row_version": int(str(form.get("row_version", "")))}
            if op == "select":
                kwargs.update(
                    image_choice_id=str(form.get("image_choice_id", "")),
                    video_choice_id=str(form.get("video_choice_id", "")),
                    confirmed_warnings=form.getlist("confirmed_warnings"),
                )
            if op in {"release-expired", "exit-without-cleanup"}:
                kwargs.update(
                    reason=str(form.get("reason", "")), confirmed=form.get("confirmed") == "yes"
                )
            with database.session() as session:
                getattr(video_service(session, request), method)(workflow_id, **kwargs)
        except (ValueError, SpecialServiceError) as exc:
            return failure(request, exc)
        return after_action(request, workflow_id, op)

    def batch(request, method: str, notice: str):
        try:
            with database.session() as session:
                result = getattr(video_service(session, request), method)()
        except SpecialServiceError as exc:
            return failure(request, exc)
        query = urlencode(
            {
                "notice": notice,
                "found": result.found,
                "queued": result.queued,
                "skipped": result.skipped,
            }
        )
        return redirect(request, f"{module_url('video_archive')}?{query}")

    @app.post(PREFIX + "/video-archive/collect-ready")
    async def collect_ready(request: Request):
        await _validated_form(request)
        return batch(request, "dispatch_ready_checks", "batch-dispatched")

    @app.post(PREFIX + "/video-archive/cleanup-completed")
    async def cleanup_completed(request: Request):
        await _validated_form(request)
        return batch(request, "dispatch_source_cleanups", "cleanup-dispatched")

    # ------------------------------------------------------------------ 手动种子
    @app.post(PREFIX + "/manual-torrent/{workflow_id:int}/choose")
    async def manual_choose(request: Request, workflow_id: int):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                module_service(session, request).action(
                    workflow_id,
                    "choose",
                    row_version=int(str(form.get("row_version", ""))),
                    inputs={
                        "choice_id": str(form.get("choice_id", "")),
                        "allow_personalized": form.get("allow_personalized") == "yes",
                    },
                )
        except ValueError as exc:
            return failure(request, exc)
        return after_action(request, workflow_id, "choose")

    # ------------------------------------------------------------------ 元数据更新
    @app.post(PREFIX + "/workflows/lanraragi_metadata/start")
    async def metadata_start(request: Request):
        form = await _validated_form(request)
        try:
            inputs = {
                "manga_ids": [v for v in re.split(r"[\s,，]+", str(form.get("manga_ids", ""))) if v]
            }
            if str(form.get("archive_id", "")).strip():
                inputs["archive_id"] = str(form["archive_id"]).strip()
            with database.session() as session:
                workflow = module_service(session, request).create("lanraragi_metadata", inputs)
        except ValueError as exc:
            return failure(request, exc)
        return created(request, workflow)

    # ------------------------------------------------------------------ 全量采集
    def full_inputs(form) -> dict:
        return {
            name: str(form[name]).strip()
            for name in ("base_url", "account", "start_id", "end_id")
            if form.get(name)
        }

    @app.post(PREFIX + "/full-collect/start")
    async def full_start(request: Request):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                workflow = module_service(session, request).create(
                    "full_collect", full_inputs(form)
                )
        except ValueError as exc:
            return failure(request, exc)
        return created(request, workflow)

    @app.get(PREFIX + "/workflow/{workflow_id:int}/logs", response_class=HTMLResponse)
    def full_logs(
        request: Request,
        workflow_id: int,
        job_id: int = 0,
        level: str = "",
        file: str = "",
        before: int | None = None,
        page: int = 1,
    ):
        from ..special.modules.full_collect.routes import log_context

        try:
            values = log_context(
                database,
                app_config,
                workflow_id,
                job_id=job_id,
                level=level,
                file=file,
                before=before,
                page=page,
            )
        except HTTPException as exc:
            return ctx.error(request, str(exc.detail), exc.status_code)
        return ctx.page(request, "special/full_collect_logs.html", **values)

    # ------------------------------------------------------------------ 报告
    @app.get(
        PREFIX + "/workflow/{workflow_id:int}/outputs/{output_id}", response_class=HTMLResponse
    )
    def report(
        request: Request,
        workflow_id: int,
        output_id: str,
        section: str = "database_only",
        page: int = 1,
    ):
        try:
            values = report_context(database, app_config, workflow_id, output_id, section, page)
        except HTTPException as exc:
            return ctx.error(request, str(exc.detail), exc.status_code)
        kind = values["workflow"].kind
        return ctx.page(
            request,
            pick(f"special/report_{kind}.html", "special/report.html"),
            **values,
            kind=kind,
            module_url=module_url(kind),
        )
