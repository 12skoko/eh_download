"""All six special modules, using the original command and report handlers."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ...db.models import SpecialWorkflow
from ...special.service import SpecialServiceError, special_module_health
from ..special_modules import get_special_module_page, special_module_cards
from .bridge import location, mount
from .common import PREFIX


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
    mount(app, ctx, "/special/workflows/{workflow_id}", PREFIX + "/workflow/{workflow_id}",
          template="uiv2/workflow.html")
    mount(app, ctx, "/special/workflows/{workflow_id}",
          PREFIX + "/workflows/{kind}/{workflow_id}", template="uiv2/workflow.html")
    mount(app, ctx, "/special/workflows/{workflow_id}",
          PREFIX + "/partials/workflow/{workflow_id}",
          template="uiv2/_workflow_panel.html", partial="uiv2/_workflow_panel.html")
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
