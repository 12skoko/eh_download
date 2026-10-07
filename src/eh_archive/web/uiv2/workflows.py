"""All six special modules, using the original command and report handlers."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ...db.models import SpecialWorkflow
from ...special.service import SpecialServiceError, special_module_health, special_workflow_detail
from ..services import Page
from ..special_modules import get_special_module_page, special_module_cards
from .bridge import location, mount
from .common import PREFIX, is_partial


def install(app, ctx):
    @app.get(PREFIX + "/workflows", response_class=HTMLResponse)
    def directory(request: Request, page: int = 1, kind: str = "", status: str = ""):
        page = max(1, page)
        statement = select(SpecialWorkflow)
        if kind:
            statement = statement.where(SpecialWorkflow.kind == kind)
        if status:
            statement = statement.where(SpecialWorkflow.status == status)
        with ctx.database.session() as session:
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            rows = list(session.scalars(statement.options(
                selectinload(SpecialWorkflow.manga_bindings)
            ).order_by(
                SpecialWorkflow.updated_at.desc(), SpecialWorkflow.id.desc()
            ).offset((page - 1) * 50).limit(50)))
        return ctx.page(request, "uiv2/workflows.html", modules=special_module_cards(ctx.config_dir),
                        workflows=rows, page=page, total=total, kind=kind, status=status)

    @app.get(PREFIX + "/workflows/{kind}", response_class=HTMLResponse)
    def module_page(request: Request, kind: str, page: int = 1, notice: str = "",
                    found: int = 0, queued: int = 0, skipped: int = 0):
        try:
            module = get_special_module_page(kind)
        except ValueError:
            return ctx.error(request, "特殊处理模块不存在", 404)
        try:
            with ctx.database.session() as session:
                data = module.load_dashboard(session, page=max(1, page))
                if kind == "full_collect":
                    from ...special.modules.full_collect.module import creation_defaults

                    data.update(creation_defaults(ctx.config_dir))
                if kind == "video_archive":
                    data.update(page=max(1, page), total=session.scalar(
                        select(func.count()).select_from(SpecialWorkflow).where(
                            SpecialWorkflow.kind == kind)))
        except SpecialServiceError as exc:
            return ctx.service_error(request, exc)
        return ctx.page(request, "uiv2/workflow_module.html", **data, module=module,
                        module_health=special_module_health(kind, ctx.config_dir),
                        per_page=200 if kind == "video_archive" else 50,
                        notice=notice, batch_found=found, batch_queued=queued, batch_skipped=skipped)

    mount(app, ctx, "/special/modules/lanraragi_metadata/mismatch-ids",
          PREFIX + "/workflows/lanraragi_metadata/mismatch-ids")
    @app.get(PREFIX + "/workflow/{workflow_id}", response_class=HTMLResponse)
    @app.get(PREFIX + "/workflows/{kind}/{workflow_id}", response_class=HTMLResponse)
    @app.get(PREFIX + "/partials/workflow/{workflow_id}", response_class=HTMLResponse)
    def workflow_page(
        request: Request, workflow_id: int, kind: str = "", page: int = 1,
        jobs_page: int | None = None, events_page: int | None = None,
        tab: str = "jobs", notice: str | None = None,
    ):
        try:
            with ctx.database.session() as session:
                detail = special_workflow_detail(
                    session, workflow_id,
                    jobs_page=page if jobs_page is None else jobs_page,
                    events_page=page if events_page is None else events_page,
                )
            if kind and kind != detail["workflow"].kind:
                return ctx.error(request, "工作流不属于此模块", 404)
            health = special_module_health(detail["workflow"].kind, ctx.config_dir)
        except SpecialServiceError as exc:
            return ctx.service_error(request, exc)
        paging = detail["history_paging"]
        root = f"{PREFIX}/workflow/{workflow_id}"
        params = {"jobs_page": paging["jobs_page"], "events_page": paging["events_page"],
                  "tab": tab if tab in {"jobs", "events"} else "jobs"}
        query = urlencode(params)
        detail.update(
            jobs_pagination=Page(detail["jobs"], paging["jobs_page"], 20, paging["jobs_total"]),
            events_pagination=Page(detail["events"], paging["events_page"], 30, paging["events_total"]),
            history_tab=params["tab"],
            history_refresh_url=f"{PREFIX}/partials/workflow/{workflow_id}?{query}",
            jobs_tab_url=root + "?" + urlencode({**params, "tab": "jobs"}),
            events_tab_url=root + "?" + urlencode({**params, "tab": "events"}),
            jobs_pager_url=root + "?" + urlencode({"events_page": paging["events_page"], "tab": "jobs"}),
            events_pager_url=root + "?" + urlencode({"jobs_page": paging["jobs_page"], "tab": "events"}),
        )
        if request.url.path.startswith(PREFIX + "/partials/") or is_partial(request, "workflow-panel"):
            return ctx.partial(request, "uiv2/_workflow_panel.html", **detail,
                               notice=notice, module_health=health)
        return ctx.page(request, "uiv2/workflow.html", **detail, notice=notice, module_health=health)

    mount(app, ctx, "/special/full-collect/{workflow_id}/logs",
          PREFIX + "/workflow/{workflow_id}/logs", template="uiv2/workflow_logs.html")
    mount(app, ctx, "/special/workflows/{workflow_id}/outputs/{output_id}",
          PREFIX + "/workflow/{workflow_id}/outputs/{output_id}", template="uiv2/workflow_report.html")
    # Explicitly adapt every old special form route; all mutations keep the
    # original CSRF, version, lease, module and workflow-kind validation.
    sources = [r for r in list(app.routes) if getattr(r, "path", "").startswith("/special/")]
    for route in sources:
        if route.path in {"/special/start/{manga_id:path}",
                          "/special/manual-torrent/start/{manga_id:path}"}:
            continue  # Already adapted by archives.install.
        if "POST" in getattr(route, "methods", set()) or route.path.endswith("/download"):
            mount(app, ctx, route.path, location(route.path))
