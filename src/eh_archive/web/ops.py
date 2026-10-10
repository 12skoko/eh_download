"""Events, log browser, configuration editor and system management pages."""

from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..management import ManagementError
from ..management.config import load_management_config
from .shared import _filter_query, _validated_form
from .configuration import (
    POLICY_LABELS,
    ConfigurationConflict,
    ConfigurationError,
    load_config_sections,
    update_config_section,
)
from .logs import list_logs, resolve_log
from .services import COMPONENT_LABELS, list_events
from .common import PREFIX, UIContext

SAVE_MESSAGES = {
    "next_worker": "已保存。下一次 Worker 启动时生效，当前任务继续使用原配置。",
    "supervisor": "已保存。需要重启 Supervisor 后生效。",
    "web": "已保存。需要重启 Web 后生效。",
    "web_and_supervisor": "已保存。需要重启 Web 和 Supervisor 后生效。",
    "none": "配置没有变化。",
}
EVENT_COMPONENTS = sorted({*COMPONENT_LABELS, "web", "collect", "supervisor"})


def install(app, ctx: UIContext) -> None:
    database = ctx.database
    config_dir = ctx.config_dir
    log_root = ctx.app_config.log_dir.resolve()

    # ------------------------------------------------------------------ 事件
    @app.get(PREFIX + "/events", response_class=HTMLResponse)
    def events(
        request: Request,
        manga_id: str | None = None,
        component: str | None = None,
        operation: str | None = None,
        error_only: bool = False,
        limit: int = 100,
        page: int = 1,
    ):
        with database.session() as session:
            result = list_events(
                session,
                manga_id=manga_id,
                component=component,
                operation=operation,
                error_only=error_only,
                limit=limit,
                page=page,
            )
        params = [
            (k, v)
            for k, v in (("manga_id", manga_id), ("component", component), ("operation", operation))
            if v
        ]
        if error_only:
            params.append(("error_only", "true"))
        params.append(("limit", str(limit)))
        return ctx.page(
            request,
            "events.html",
            page=result,
            manga_id=manga_id or "",
            component=component or "",
            operation=operation or "",
            error_only=error_only,
            limit=limit,
            filter_qs=_filter_query(params),
            event_components=EVENT_COMPONENTS,
        )

    # ------------------------------------------------------------------ 日志
    @app.get(PREFIX + "/logs", response_class=HTMLResponse)
    def logs(request: Request, q: str = "", directory: str = "", page: int = Query(1, ge=1)):
        directory = "" if directory == "." else directory
        try:
            rows, truncated = list_logs(log_root, q, directory)
        except HTTPException as exc:
            return ctx.error(request, str(exc.detail), exc.status_code)
        rows.sort(
            key=lambda row: (
                not row["is_directory"],
                0 if row["is_directory"] else -row["mtime"],
                row["name"],
            )
        )
        parts = PurePosixPath(directory).parts
        breadcrumbs = [
            {"name": part, "url": quote("/".join(parts[: i + 1]), safe="")}
            for i, part in enumerate(parts)
        ]
        pages = max(1, (len(rows) + 99) // 100)
        page = min(page, pages)
        return ctx.page(
            request,
            "logs.html",
            headers={"Cache-Control": "no-store"},
            rows=rows[(page - 1) * 100 : page * 100],
            q=q,
            directory=directory,
            filter_query=urlencode({"q": q, "directory": directory}),
            parent_query=urlencode({"directory": "/".join(parts[:-1])}),
            breadcrumbs=breadcrumbs,
            page=page,
            pages=pages,
            total=len(rows),
            truncated=truncated,
            available=log_root.is_dir(),
        )

    @app.get(PREFIX + "/logs/view", response_class=HTMLResponse)
    def log_view(request: Request, file: str):
        try:
            resolve_log(log_root, file)
        except HTTPException as exc:
            return ctx.error(request, str(exc.detail), exc.status_code)
        parent = str(PurePosixPath(file).parent)
        parts = [] if parent == "." else PurePosixPath(parent).parts
        return ctx.page(
            request,
            "log_view.html",
            headers={"Cache-Control": "no-store"},
            filename=file,
            file_query=quote(file, safe=""),
            directory_query=urlencode({"directory": parent}),
            breadcrumbs=[
                {"name": part, "url": quote("/".join(parts[: i + 1]), safe="")}
                for i, part in enumerate(parts)
            ],
        )

    # ------------------------------------------------------------------ 配置
    def render_settings(request, selected, *, saved=None, error=None, form=None, status=200):
        sections = load_config_sections(config_dir)
        active = next((s for s in sections if s.name == selected), sections[0])
        if isinstance(error, ConfigurationConflict) and form is not None:
            active = replace(active, revision=str(form.get("revision", "")))
        if form is not None:
            active = replace(
                active,
                fields=tuple(
                    replace(
                        field,
                        value=str(form.get(field.name, field.value)) if not field.secret else "",
                        checked=field.name in form if field.editable else field.checked,
                        error=error.fields.get(field.name, "") if error else "",
                    )
                    for field in active.fields
                ),
            )
        return ctx.page(
            request,
            "settings.html",
            status_code=status,
            sections=sections,
            active_section=active,
            config_error=str(error) if error else active.error,
            notice=SAVE_MESSAGES.get(saved),
            saved_policy=saved,
            policy_labels=POLICY_LABELS,
        )

    @app.get(PREFIX + "/settings", response_class=HTMLResponse)
    def settings(request: Request, section: str = "app", saved: str | None = None):
        return render_settings(request, section, saved=saved)

    @app.post(PREFIX + "/settings/{section_name}")
    async def save_settings(request: Request, section_name: str):
        form = await _validated_form(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            result = update_config_section(
                config_dir, section_name, form, revision=str(form.get("revision", ""))
            )
        except ConfigurationError as exc:
            status = 409 if isinstance(exc, ConfigurationConflict) else 422
            if wants_json:
                return JSONResponse({"detail": str(exc), "fields": exc.fields}, status_code=status)
            return render_settings(request, section_name, error=exc, form=form, status=status)
        target = f"{PREFIX}/settings?" + urlencode(
            {
                "section": section_name,
                "saved": result.restart if result.changed_fields else "none",
            }
        )
        if wants_json:
            return JSONResponse(
                {
                    "redirect": target,
                    "changed_fields": result.changed_fields,
                    "policy": result.restart,
                }
            )
        from .common import redirect

        return redirect(request, target)

    # ------------------------------------------------------------------ 系统
    def management_error() -> str | None:
        try:
            load_management_config(ctx.management_path)
        except ManagementError as exc:
            return str(exc)
        return None

    @app.get(PREFIX + "/system", response_class=HTMLResponse)
    def system(request: Request):
        return ctx.page(request, "system.html", management_error=management_error())

    @app.get(PREFIX + "/system/operations/{identifier}", response_class=HTMLResponse)
    def operation(request: Request, identifier: str):
        return ctx.page(
            request,
            "system_operation.html",
            identifier=identifier,
            management_error=management_error(),
        )
