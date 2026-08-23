from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..config import load_config
from ..config.loader import SUPERVISOR_MODULES
from ..db import Database
from ..db.models import EventLog, MangaRecord, SystemControl, SystemHealth
from ..logging import configure_logging
from .auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    SessionSigner,
    WebIdentity,
    valid_password_hash,
)
from .configuration import (
    ConfigurationConflict,
    ConfigurationError,
    load_config_sections,
    update_config_section,
)
from .services import (
    COMPONENT_LABELS,
    CONTROL_COMPONENTS,
    MANUAL_STATUS_TARGETS,
    STATUS_LABELS,
    Conflict,
    InvalidRequest,
    WebService,
    WebServiceError,
    allowed_actions,
    dashboard_data,
    list_events,
    list_manga,
    list_review_manga,
    manga_detail,
    review_facets,
    safe_detail,
    serialize_manga,
    serialize_model,
)

TEMPLATE_DIR = Path(__file__).with_name("templates")
STATIC_DIR = Path(__file__).with_name("static")


def _gallery_id(value: str) -> str | None:
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or hostname not in {
        "e-hentai.org",
        "www.e-hentai.org",
        "exhentai.org",
        "www.exhentai.org",
    }:
        return None
    match = re.fullmatch(r"/g/(\d+/[\w-]+)/?", parsed.path)
    return match.group(1) if match else None


def create_app(database: Database | None = None, *, config_dir: str | Path = "config"):
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
        from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
        from fastapi.staticfiles import StaticFiles
        from fastapi.templating import Jinja2Templates
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError("Install eh-archive to use the Web process") from exc

    config_dir = Path(config_dir)
    app_config, supervisor_config, _, secrets_config = load_config(config_dir)
    database = database or Database(app_config.database_url)
    auth_enabled = bool(secrets_config.web_password_hash)
    if auth_enabled and not valid_password_hash(secrets_config.web_password_hash):
        raise RuntimeError("web_password_hash is invalid; generate it with eharchive web-password")
    if auth_enabled and not secrets_config.web_secret:
        raise RuntimeError("web_secret is required when web_password_hash is configured")
    if not auth_enabled and not _is_loopback(app_config.web_host):
        raise RuntimeError(
            "Web login must be configured before listening outside localhost; "
            "set web_username, web_password_hash and web_secret in secrets.toml"
        )
    signer = SessionSigner(secrets_config.web_secret) if auth_enabled else None

    app = FastAPI(title="EH Archive", version="6.0.0")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["datetime"] = _format_datetime
    templates.env.filters["filesize"] = _format_filesize
    templates.env.filters["status_label"] = lambda value: STATUS_LABELS.get(value, value)
    templates.env.filters["component_label"] = lambda value: COMPONENT_LABELS.get(value, value)
    templates.env.filters["safe_detail"] = safe_detail
    templates.env.filters["error_summary"] = _error_summary
    templates.env.filters["manga_tab_id"] = _manga_tab_id
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.database = database
    app.state.auth_enabled = auth_enabled
    app.state.config_dir = config_dir

    class ManualManga(BaseModel):
        url: str
        priority: int = Field(default=100, ge=-100000, le=100000)
        remark: str | None = None
        row_version: int | None = None

    class RemarkUpdate(BaseModel):
        remark: str | None = None
        row_version: int

    class PriorityUpdate(BaseModel):
        priority: int = Field(ge=-100000, le=100000)
        row_version: int

    class ControlUpdate(BaseModel):
        state: str
        reason: str | None = None
        row_version: int | None = None

    class ActionUpdate(BaseModel):
        row_version: int
        reason: str | None = None
        archive_id: str | None = None

    class StatusOverrideUpdate(BaseModel):
        row_version: int
        reason: str | None = None
        download_method: str | None = None
        artifact_filename: str | None = None
        archive_id: str | None = None
        superseded_by_id: str | None = None

    status_query = Query(default=[])

    @app.middleware("http")
    async def authenticate(request, call_next):
        path = request.url.path
        public = path == "/login" or path == "/health/live" or path.startswith("/static/")
        if public or not auth_enabled:
            request.state.identity = WebIdentity("local", "local", int(time.time()) + 3600)
            request.state.auth_via_bearer = False
            return await call_next(request)

        identity = None
        via_bearer = False
        authorization = request.headers.get("authorization", "")
        if secrets_config.web_secret and authorization == f"Bearer {secrets_config.web_secret}":
            identity = WebIdentity("api", "", int(time.time()) + 60)
            via_bearer = True
        elif signer is not None:
            identity = signer.verify(request.cookies.get(SESSION_COOKIE))
        if identity is None:
            if path.startswith("/api/") or path == "/health":
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            next_path = request.url.path + ("?" + request.url.query if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(next_path, safe='/?=&')}", status_code=303)
        request.state.identity = identity
        request.state.auth_via_bearer = via_bearer
        if (
            path.startswith("/api/")
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and not via_bearer
            and request.headers.get("x-csrf-token") != identity.csrf_token
        ):
            return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", error: str | None = None):
        if auth_enabled and signer and signer.verify(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse(_safe_next(next), status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next": _safe_next(next), "error": error, "auth_enabled": auth_enabled},
        )

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        next_path = _safe_next(str(form.get("next", "/")))
        from .auth import verify_password

        valid = (
            auth_enabled
            and username == secrets_config.web_username
            and verify_password(password, secrets_config.web_password_hash)
        )
        if not valid:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"next": next_path, "error": "用户名或密码错误", "auth_enabled": True},
                status_code=401,
            )
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            signer.create(username),
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=False,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        await _validated_form(request)
        response = _redirect_response(request, "/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/health/live")
    def liveness():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with database.session() as session:
            data = dashboard_data(session)
        health_states = {
            component: _health_status(item, supervisor_config.health_check_interval_seconds)
            for component, item in data["health"].items()
        }
        supervisor_state = _supervisor_status(
            data["controls"].get("supervisor"), supervisor_config.poll_seconds
        )
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=_context(
                request,
                **data,
                health_states=health_states,
                supervisor_state=supervisor_state,
            ),
        )

    @app.get("/manga", response_class=HTMLResponse)
    def manga_queue(
        request: Request,
        status: list[str] = status_query,
        q: str | None = None,
        queue_source: str | None = None,
        has_error: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ):
        error_filter = None if has_error not in {"yes", "no"} else has_error == "yes"
        try:
            with database.session() as session:
                page = list_manga(
                    session,
                    statuses=status,
                    query_text=q,
                    queue_source=queue_source,
                    has_error=error_filter,
                    limit=limit,
                    cursor=cursor,
                )
        except WebServiceError as exc:
            return _error_response(request, templates, exc)
        return templates.TemplateResponse(
            request=request,
            name=(
                "_manga_results.html"
                if request.headers.get("HX-Target") == "manga-results"
                else "manga/list.html"
            ),
            context=_context(
                request,
                page=page,
                selected_statuses=status,
                q=q or "",
                queue_source=queue_source or "",
                has_error=has_error or "",
                limit=limit,
            ),
        )

    @app.post("/manga/add")
    async def add_manga_page(request: Request):
        form = await _validated_form(request)
        url = str(form.get("url", "")).strip()
        manga_id = _gallery_id(url)
        if manga_id is None:
            return _error_response(
                request, templates, InvalidRequest("URL 中没有有效的 EH 画廊 ID")
            )
        try:
            with database.session() as session:
                row = WebService(session, actor=_actor(request)).add_manga(
                    url=url,
                    manga_id=manga_id,
                    priority=int(str(form.get("priority", "100"))),
                    remark=_optional_text(form.get("remark")),
                    row_version=_optional_int(form.get("row_version")),
                )
        except (ValueError, WebServiceError) as exc:
            error = exc if isinstance(exc, WebServiceError) else InvalidRequest("优先级必须是整数")
            return _error_response(request, templates, error)
        return _redirect_response(request, f"/manga/{row.manga_id}?notice=added")

    @app.get("/review", response_class=HTMLResponse)
    def review_page(
        request: Request,
        status: str = "manual_review",
        q: str | None = None,
        error_code: str | None = None,
        operation: str | None = None,
        cursor: str | None = None,
    ):
        if status not in {"manual_review", "quarantined"}:
            status = "manual_review"
        try:
            with database.session() as session:
                page = list_review_manga(
                    session,
                    status=status,
                    query_text=q,
                    error_code=error_code,
                    operation=operation,
                    limit=50,
                    cursor=cursor,
                )
                error_facets, operations = review_facets(
                    session,
                    status=status,
                    query_text=q,
                    operation=operation,
                )
        except WebServiceError as exc:
            return _error_response(request, templates, exc)
        return templates.TemplateResponse(
            request=request,
            name=(
                "_review_workspace.html"
                if request.headers.get("HX-Target") == "review-workspace"
                else "review.html"
            ),
            context=_context(
                request,
                page=page,
                selected_status=status,
                q=q or "",
                error_code=error_code or "",
                operation=operation or "",
                error_facets=error_facets,
                review_operations=operations,
            ),
        )

    @app.get("/events", response_class=HTMLResponse)
    def events_page(
        request: Request,
        manga_id: str | None = None,
        component: str | None = None,
        operation: str | None = None,
        error_only: bool = False,
        limit: int = 100,
    ):
        with database.session() as session:
            events = list_events(
                session,
                manga_id=manga_id,
                component=component,
                operation=operation,
                error_only=error_only,
                limit=limit,
            )
        return templates.TemplateResponse(
            request=request,
            name="events.html",
            context=_context(
                request,
                events=events,
                manga_id=manga_id or "",
                component=component or "",
                operation=operation or "",
                error_only=error_only,
                limit=limit,
            ),
        )

    @app.get("/config", response_class=HTMLResponse)
    def config_page(
        request: Request,
        notice: str | None = None,
        updated_file: str | None = None,
    ):
        try:
            sections = load_config_sections(config_dir)
        except (ConfigurationError, OSError, TypeError, ValueError) as exc:
            return _error_response(request, templates, InvalidRequest(f"读取配置失败：{exc}"))
        return templates.TemplateResponse(
            request=request,
            name="config.html",
            context=_context(
                request,
                sections=sections,
                notice=notice,
                updated_file=updated_file,
            ),
        )

    @app.post("/config/{section_name}")
    async def update_config_page(request: Request, section_name: str):
        form = await _validated_form(request)
        try:
            result = update_config_section(
                config_dir,
                section_name,
                form,
                revision=str(form.get("revision", "")),
            )
        except ConfigurationConflict as exc:
            return _error_response(request, templates, Conflict(str(exc)))
        except ConfigurationError as exc:
            return _error_response(request, templates, InvalidRequest(str(exc)))
        if result.changed_fields:
            with database.session() as session:
                session.add(
                    EventLog(
                        manga_id=None,
                        component="web",
                        event_type="manual",
                        operation="config_update",
                        actor=_actor(request),
                        detail={
                            "file": result.filename,
                            "fields": list(result.changed_fields),
                            "restart_required": result.restart,
                        },
                    )
                )
        notice_value = "saved" if result.changed_fields else "unchanged"
        return _redirect_response(
            request,
            f"/config?notice={notice_value}&updated_file={quote(result.filename)}",
        )

    @app.get("/manga/{manga_id:path}", response_class=HTMLResponse)
    def manga_page(request: Request, manga_id: str, notice: str | None = None):
        try:
            with database.session() as session:
                detail = manga_detail(session, manga_id)
        except WebServiceError as exc:
            return _error_response(request, templates, exc)
        return templates.TemplateResponse(
            request=request,
            name="manga/detail.html",
            context=_context(request, **detail, notice=notice),
        )

    @app.post("/manga/{manga_id:path}/remark")
    async def update_remark_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.update_remark(
                manga_id,
                remark=_optional_text(form.get("remark")),
                row_version=int(str(form.get("row_version", ""))),
            ),
            "remark-updated",
        )

    @app.post("/manga/{manga_id:path}/priority")
    async def update_priority_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.update_priority(
                manga_id,
                priority=int(str(form.get("priority", ""))),
                row_version=int(str(form.get("row_version", ""))),
            ),
            "priority-updated",
        )

    @app.post("/manga/{manga_id:path}/actions/{action}")
    async def manga_action_page(request: Request, manga_id: str, action: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.action(
                manga_id,
                action,
                row_version=int(str(form.get("row_version", ""))),
                reason=_optional_text(form.get("reason")),
                archive_id=_optional_text(form.get("archive_id")),
            ),
            "action-completed",
        )

    @app.post("/manga/{manga_id:path}/lease/release-expired")
    async def release_expired_lease_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.release_expired_lease(
                manga_id,
                row_version=int(str(form.get("row_version", ""))),
                reason=_optional_text(form.get("reason")),
                confirmed=form.get("confirmed") == "yes",
            ),
            "expired-lease-released",
        )

    @app.post("/manga/{manga_id:path}/status/{target_status}")
    async def override_manga_status_page(request: Request, manga_id: str, target_status: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.override_status(
                manga_id,
                target_status=target_status,
                row_version=int(str(form.get("row_version", ""))),
                reason=_optional_text(form.get("reason")),
                download_method=_optional_text(form.get("download_method")),
                artifact_filename=_optional_text(form.get("artifact_filename")),
                archive_id=_optional_text(form.get("archive_id")),
                superseded_by_id=_optional_text(form.get("superseded_by_id")),
                confirmation_manga_id=_optional_text(form.get("confirmation_manga_id")),
                allow_web_only=True,
            ),
            "status-updated",
            app_config=app_config,
        )

    @app.post("/control/{component}")
    async def control_page(request: Request, component: str):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                control = WebService(session, actor=_actor(request)).set_control(
                    component,
                    state=str(form.get("state", "")),
                    reason=_optional_text(form.get("reason")),
                    row_version=_optional_int(form.get("row_version")),
                )
        except (ValueError, WebServiceError) as exc:
            error = exc if isinstance(exc, WebServiceError) else InvalidRequest("表单数据无效")
            if request.headers.get("HX-Request") == "true":
                with database.session() as session:
                    current = session.get(SystemControl, component)
                return templates.TemplateResponse(
                    request=request,
                    name="_component_row.html",
                    context=_context(
                        request,
                        component=component,
                        control=current,
                        component_error=str(error),
                    ),
                )
            return _error_response(request, templates, error)
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request=request,
                name="_component_row.html",
                context=_context(
                    request,
                    component=component,
                    control=control,
                    component_error=None,
                ),
            )
        return _redirect_response(request, "/?notice=control-updated")

    @app.get("/health")
    def health():
        try:
            database.ping()
            with database.session() as session:
                controls = {
                    row.component: {
                        "state": row.state,
                        "reason": row.reason,
                        "heartbeat_at": row.heartbeat_at,
                        "row_version": row.row_version,
                    }
                    for row in session.scalars(select(SystemControl))
                }
                snapshots = {
                    row.component: {
                        "status": _health_status(
                            row, supervisor_config.health_check_interval_seconds
                        ),
                        "reported_status": row.status,
                        "checked_at": row.checked_at,
                        "latency_ms": row.latency_ms,
                        "error_code": row.error_code,
                        "message": row.message,
                        "detail": safe_detail(row.detail),
                    }
                    for row in session.scalars(select(SystemHealth))
                }
                counts = dashboard_data(session)["counts"]
            return {
                "ok": True,
                "database": True,
                "components": controls,
                "health": snapshots,
                "counts": counts,
            }
        except (SQLAlchemyError, OSError) as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "database": False,
                    "error": type(exc).__name__,
                    "components": {},
                    "health": {},
                    "counts": {},
                },
                status_code=503,
            )

    @app.get("/api/manga")
    def api_list_manga(
        status: str | None = None,
        q: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        offset: int = 0,
    ):
        try:
            with database.session() as session:
                page = list_manga(
                    session,
                    statuses=[status] if status else None,
                    query_text=q,
                    limit=limit,
                    cursor=cursor,
                    offset=offset,
                )
                return [serialize_manga(row) for row in page.rows]
        except WebServiceError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.get("/api/manga/{manga_id:path}")
    def api_get_manga(manga_id: str):
        try:
            with database.session() as session:
                detail = manga_detail(session, manga_id)
                value = serialize_manga(detail["row"])
                value["info"] = serialize_model(detail["row"].info) if detail["row"].info else None
                value["attempts"] = [
                    {**serialize_model(item), "detail": safe_detail(item.detail)}
                    for item in detail["attempts"]
                ]
                value["events"] = [
                    {**serialize_model(item), "detail": safe_detail(item.detail)}
                    for item in detail["events"]
                ]
                return value
        except WebServiceError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.post("/api/manga", status_code=201)
    def api_add_manga(request: Request, payload: ManualManga):
        manga_id = _gallery_id(payload.url)
        if manga_id is None:
            raise HTTPException(400, "URL does not contain an EH gallery id")
        try:
            with database.session() as session:
                row = WebService(session, actor=_actor(request)).add_manga(
                    url=payload.url,
                    manga_id=manga_id,
                    priority=payload.priority,
                    remark=payload.remark,
                    row_version=payload.row_version,
                )
                return serialize_manga(row)
        except WebServiceError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.patch("/api/manga/{manga_id:path}/remark")
    def api_update_remark(request: Request, manga_id: str, payload: RemarkUpdate):
        return _api_update(
            database,
            request,
            lambda service: service.update_remark(
                manga_id, remark=payload.remark, row_version=payload.row_version
            ),
        )

    @app.patch("/api/manga/{manga_id:path}/priority")
    def api_update_priority(request: Request, manga_id: str, payload: PriorityUpdate):
        return _api_update(
            database,
            request,
            lambda service: service.update_priority(
                manga_id, priority=payload.priority, row_version=payload.row_version
            ),
        )

    @app.post("/api/manga/{manga_id:path}/actions/{action}")
    def api_action(request: Request, manga_id: str, action: str, payload: ActionUpdate):
        return _api_update(
            database,
            request,
            lambda service: service.action(
                manga_id,
                action,
                row_version=payload.row_version,
                reason=payload.reason,
                archive_id=payload.archive_id,
            ),
        )

    @app.post("/api/manga/{manga_id:path}/archive-confirmation")
    def api_confirm_archive(request: Request, manga_id: str, payload: ActionUpdate):
        return _api_update(
            database,
            request,
            lambda service: service.action(
                manga_id,
                "confirm-uploaded",
                row_version=payload.row_version,
                reason=payload.reason,
                archive_id=payload.archive_id,
            ),
        )

    @app.post("/api/manga/{manga_id:path}/status/{target_status}")
    def api_override_manga_status(
        request: Request,
        manga_id: str,
        target_status: str,
        payload: StatusOverrideUpdate,
    ):
        return _api_update(
            database,
            request,
            lambda service: service.override_status(
                manga_id,
                target_status=target_status,
                row_version=payload.row_version,
                reason=payload.reason,
                download_method=payload.download_method,
                artifact_filename=payload.artifact_filename,
                archive_id=payload.archive_id,
                superseded_by_id=payload.superseded_by_id,
            ),
            app_config=app_config,
        )

    control_body = Body()

    @app.put("/api/control/{component}")
    def api_control(request: Request, component: str, payload=control_body):
        try:
            value = ControlUpdate(**payload)
            with database.session() as session:
                row = WebService(session, actor=_actor(request)).set_control(
                    component,
                    state=value.state,
                    reason=value.reason,
                    row_version=value.row_version,
                )
                return {
                    "component": row.component,
                    "state": row.state,
                    "reason": row.reason,
                    "row_version": row.row_version,
                }
        except WebServiceError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"invalid control payload: {exc}") from exc

    return app


def _context(request, **values):
    identity = getattr(request.state, "identity", WebIdentity("local", "local", 0))
    return {
        "identity": identity,
        "csrf_token": identity.csrf_token,
        "status_labels": STATUS_LABELS,
        "component_labels": COMPONENT_LABELS,
        "control_components": CONTROL_COMPONENTS,
        "supervisor_modules": SUPERVISOR_MODULES,
        "allowed_actions": allowed_actions,
        "manual_status_targets": MANUAL_STATUS_TARGETS,
        "now": datetime.now(UTC),
        **values,
    }


async def _validated_form(request):
    form = await request.form()
    identity = getattr(request.state, "identity", None)
    if identity is None or str(form.get("csrf_token", "")) != identity.csrf_token:
        from fastapi import HTTPException

        raise HTTPException(403, "CSRF validation failed")
    return form


def _actor(request) -> str:
    return f"web:{request.state.identity.username}"


def _page_update(
    request,
    templates,
    database,
    manga_id,
    callback,
    notice,
    *,
    app_config=None,
):
    try:
        with database.session() as session:
            callback(WebService(session, actor=_actor(request), app_config=app_config))
    except (ValueError, WebServiceError) as exc:
        error = exc if isinstance(exc, WebServiceError) else InvalidRequest("表单数据无效")
        return _error_response(request, templates, error)
    return _redirect_response(request, f"/manga/{manga_id}?notice={notice}")


def _api_update(database, request, callback, *, app_config=None):
    from fastapi import HTTPException

    try:
        with database.session() as session:
            return serialize_manga(
                callback(WebService(session, actor=_actor(request), app_config=app_config))
            )
    except WebServiceError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


def _error_response(request, templates, exc: WebServiceError):
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context=_context(request, message=str(exc), status_code=exc.status_code),
        status_code=exc.status_code,
    )


def _optional_text(value) -> str | None:
    text_value = str(value).strip() if value is not None else ""
    return text_value or None


def _optional_int(value) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(str(value))


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


def _redirect_response(request, location: str):
    responses = __import__("fastapi.responses", fromlist=["Response", "RedirectResponse"])
    if request.headers.get("HX-Request") == "true":
        return responses.Response(status_code=204, headers={"HX-Redirect": location})
    return responses.RedirectResponse(location, status_code=303)


def _is_loopback(host: str) -> bool:
    return host.casefold() in {"127.0.0.1", "localhost", "::1"}


def _health_status(row: SystemHealth, interval_seconds: float) -> str:
    age = _age_seconds(row.checked_at)
    return "stale" if age > max(interval_seconds * 3, 180) else row.status


def _supervisor_status(row: SystemControl | None, poll_seconds: float) -> str:
    if row is None or row.heartbeat_at is None:
        return "unknown"
    age = _age_seconds(row.heartbeat_at)
    return "stale" if age > max(poll_seconds * 6, 30) else row.state


def _age_seconds(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (datetime.now(UTC) - value).total_seconds()


def _format_datetime(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _format_filesize(value) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


def _manga_tab_id(value) -> str:
    manga_id = str(value or "").strip()
    return manga_id.partition("/")[0] or manga_id


def _error_summary(value) -> str:
    detail = str(value or "").strip()
    if not detail:
        return "未记录原因"
    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError):
        return detail
    if isinstance(parsed, dict):
        for key in ("error", "message", "detail"):
            summary = parsed.get(key)
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
    return detail


def _serialize(row):
    if row is None:
        return None
    if isinstance(row, MangaRecord):
        return serialize_manga(row)
    return serialize_model(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-web")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    app_config, _, _, _ = load_config(args.config_dir)
    configure_logging(
        app_config.log_level,
        app_config.log_dir,
        timezone=app_config.timezone,
        component="web",
        run_id=str(uuid.uuid4()),
    )
    application = create_app(Database(app_config.database_url), config_dir=args.config_dir)
    import uvicorn

    uvicorn.run(application, host=app_config.web_host, port=app_config.web_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
