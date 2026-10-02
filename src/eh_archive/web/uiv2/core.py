"""Login, overview, scheduler controls and global search."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ...config.loader import SUPERVISOR_MODULES
from ...db.models import SystemControl
from ...management import ManagementError
from ...supervisor.scheduling import (
    COOLDOWN_MODULES,
    INTERVAL_MODULES,
    module_view,
    request_cooldown_release,
    request_run,
)
from ..app import (
    _actor,
    _health_status,
    _optional_int,
    _optional_text,
    _safe_next,
    _supervisor_status,
    _validated_form,
)
from ..auth import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS
from ..services import (
    COMPONENT_LABELS,
    InvalidRequest,
    WebService,
    WebServiceError,
    dashboard_data,
    list_manga,
    review_facets,
    running_attempts,
    running_module_tasks,
)
from .common import PREFIX, Uiv2, is_htmx, redirect, trigger_header


def _safe_v2_next(value: str) -> str:
    target = _safe_next(value)
    return target if target.startswith(PREFIX) else PREFIX + "/"


def control_state(ctx: Uiv2, session) -> dict:
    """Supervisor and per-module view shared by the overview, the pill and polling."""
    controls = {row.component: row for row in session.scalars(select(SystemControl))}
    supervisor = controls.get("supervisor")
    tasks = running_module_tasks(session)
    running = {task.module for task in tasks if task.module in SUPERVISOR_MODULES}
    if any(task.module not in SUPERVISOR_MODULES and task.started_at for task in tasks):
        running.add("special_processing")
    running |= {name for name, row in controls.items() if row.schedule_running}
    units = {}
    for name in SUPERVISOR_MODULES:
        row = controls.get(name)
        schedule = module_view(
            name, row, supervisor, ctx.supervisor_config, ctx.app_config.timezone
        ) if name in COOLDOWN_MODULES else {"interval": False}
        paused = bool(row and row.state == "paused")
        state = (
            "paused" if paused else "cooldown" if schedule.get("cooldown_until")
            else "running" if name in running else "idle"
        )
        units[name] = {
            "name": name, "label": COMPONENT_LABELS.get(name, name), "control": row,
            "schedule": schedule, "state": state, "interval": name in INTERVAL_MODULES,
            "enabled": ctx.supervisor_config.modules.get(name, True),
        }
    return {
        "units": units,
        "supervisor": supervisor,
        "supervisor_state": _supervisor_status(supervisor, ctx.supervisor_config.poll_seconds),
        "running_tasks": tasks,
    }


def activity_items(session) -> list[dict]:
    """Merge module tasks and running attempts so each worker appears once."""
    attempts = {(a.manga_id, a.operation): a for a in running_attempts(session, limit=24)}
    items = []
    for task in running_module_tasks(session):
        items.append({"task": task, "attempt": attempts.get((task.manga_id, task.module))})
    return items


def install(app, ctx: Uiv2) -> None:
    templates = ctx.templates
    database = ctx.database

    @app.get(PREFIX + "/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = PREFIX + "/", error: str | None = None):
        if ctx.auth_enabled and ctx.signer and ctx.signer.verify(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse(_safe_v2_next(next), status_code=303)
        return templates.TemplateResponse(
            request=request, name="uiv2/login.html",
            context={"next": _safe_v2_next(next), "error": error, "auth_enabled": ctx.auth_enabled},
        )

    @app.post(PREFIX + "/login")
    async def login(request: Request):
        from ..auth import verify_password

        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        next_path = _safe_v2_next(str(form.get("next", PREFIX + "/")))
        secrets = ctx.secrets_config
        valid = (
            ctx.auth_enabled
            and username == secrets.web_username
            and verify_password(password, secrets.web_password_hash)
        )
        if not valid:
            return templates.TemplateResponse(
                request=request, name="uiv2/login.html", status_code=401,
                context={"next": next_path, "error": "用户名或密码错误", "auth_enabled": True},
            )
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE, ctx.signer.create(username), max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True, secure=False, samesite="lax", path="/",
        )
        return response

    @app.post(PREFIX + "/logout")
    async def logout(request: Request):
        await _validated_form(request)
        response = redirect(request, PREFIX + "/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get(PREFIX, include_in_schema=False)
    def root_redirect():
        return RedirectResponse(PREFIX + "/", status_code=307)

    @app.get(PREFIX + "/", response_class=HTMLResponse)
    def overview(request: Request):
        with database.session() as session:
            data = dashboard_data(session)
            state = control_state(ctx, session)
            review, _ = review_facets(session, status="manual_review")
            quarantine, _ = review_facets(session, status="quarantined")
            activity = activity_items(session)
        health_states = {
            component: _health_status(item, ctx.supervisor_config.health_check_interval_seconds)
            for component, item in data["health"].items()
        }
        return ctx.page(
            request, "uiv2/overview.html", **data, **state, activity=activity,
            health_states=health_states, review_facets=review[:4],
            quarantine_facets=quarantine[:3],
        )

    @app.get(PREFIX + "/partials/activity", response_class=HTMLResponse)
    def activity_partial(request: Request):
        with database.session() as session:
            items = activity_items(session)
        return ctx.partial(request, "uiv2/_activity.html", activity=items)

    def control_response(request, scope: str, headers=None):
        with database.session() as session:
            state = control_state(ctx, session)
        return ctx.partial(
            request, "uiv2/_control_oob.html", scope=scope, headers=headers, **state
        )

    @app.get(PREFIX + "/partials/control", response_class=HTMLResponse)
    def control_partial(request: Request, scope: str = "pill"):
        return control_response(request, "overview" if scope == "overview" else "pill")

    def scope_of(form) -> str:
        return "overview" if form.get("scope") == "overview" else "pill"

    @app.post(PREFIX + "/control/{component}")
    async def control(request: Request, component: str):
        from pathlib import Path

        from ...management.service import control_guard

        form = await _validated_form(request)
        state = str(form.get("state", ""))
        try:
            with control_guard(component, Path(ctx.management_path)), database.session() as session:
                WebService(session, actor=_actor(request)).set_control(
                    component, state=state, reason=_optional_text(form.get("reason")),
                    row_version=_optional_int(form.get("row_version")),
                )
        except ManagementError as exc:
            return ctx.error(request, str(exc), 409)
        except (ValueError, WebServiceError) as exc:
            error = exc if isinstance(exc, WebServiceError) else InvalidRequest("表单数据无效")
            return ctx.service_error(request, error)
        label = COMPONENT_LABELS.get(component, component)
        message = f"{label}已{'恢复调度' if state == 'running' else '暂停' if state == 'paused' else '进入排空'}"
        if not is_htmx(request):
            return redirect(request, PREFIX + "/", "control-updated")
        return control_response(request, scope_of(form), trigger_header(toast=message))

    @app.post(PREFIX + "/control/{component}/run")
    async def run_module(request: Request, component: str):
        form = await _validated_form(request)
        try:
            if form.get("confirmed") != "yes":
                raise ValueError("请先确认手动运行")
            with database.session() as session:
                request_run(
                    session, component, owner=str(form.get("owner", "")),
                    request_version=str(form.get("request_version", "")),
                    actor=_actor(request), config=ctx.supervisor_config,
                    timezone=ctx.app_config.timezone,
                )
        except ValueError as exc:
            return ctx.error(request, str(exc), 400)
        if not is_htmx(request):
            return redirect(request, PREFIX + "/", "control-updated")
        label = COMPONENT_LABELS.get(component, component)
        return control_response(request, scope_of(form), trigger_header(
            toast=f"已请求执行{label}；启动后会重新计算下次计划时间"
        ))

    @app.post(PREFIX + "/control/{component}/release-cooldown")
    async def release_cooldown(request: Request, component: str):
        form = await _validated_form(request)
        try:
            if form.get("confirmed") != "yes":
                raise ValueError("请先确认解除冷却")
            with database.session() as session:
                request_cooldown_release(
                    session, component, owner=str(form.get("owner", "")),
                    version=str(form.get("cooldown_version", "")), actor=_actor(request),
                    config=ctx.supervisor_config, timezone=ctx.app_config.timezone,
                )
        except ValueError as exc:
            return ctx.error(request, str(exc), 400)
        if not is_htmx(request):
            return redirect(request, PREFIX + "/", "control-updated")
        return control_response(request, scope_of(form), trigger_header(
            toast="已提交解除冷却请求，等待 Supervisor 处理"
        ))

    @app.get(PREFIX + "/search", response_class=HTMLResponse)
    def search(request: Request, q: str = ""):
        rows = []
        text = q.strip()
        if text:
            with database.session() as session:
                rows = list_manga(session, query_text=text, limit=8, page=1).rows
        return ctx.partial(request, "uiv2/_search_results.html", rows=rows, q=text)
