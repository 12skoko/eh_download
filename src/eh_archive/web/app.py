from __future__ import annotations

import argparse
import hashlib
import time
import uuid
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..config import load_config
from ..db import Database
from ..db.models import SystemControl, SystemHealth
from ..logging import configure_logging, get_logger
from ..management.config_migrations import migrate_configuration
from ..special.remarks import PHASE_LABELS, user_remark
from .auth import SESSION_COOKIE, SessionSigner, WebIdentity, valid_password_hash
from .services import (
    COMPONENT_LABELS,
    STATUS_LABELS,
    WebService,
    WebServiceError,
    bulk_override_status,
    dashboard_data,
    list_manga,
    manga_detail,
    safe_detail,
    serialize_manga,
    serialize_model,
)

from .shared import (
    _context,
    _actor,
    _api_update,
    _is_loopback,
    _health_status,
    _format_collected_at,
    _format_full_collect_datetime,
    _format_datetime,
    _format_filesize,
    _format_duration,
    _attempt_progress,
    _manga_tab_id,
    _error_summary,
)

TEMPLATE_DIR = Path(__file__).with_name("templates")
STATIC_DIR = Path(__file__).with_name("static")


class BulkStatusUpdate(BaseModel):
    items: list[tuple[str, int]] = Field(min_length=1, max_length=100)
    target_status: str
    reason: str | None = Field(default=None, max_length=4000)
    download_method: str | None = None
    superseded_by_id: str | None = None


def create_app(
    database: Database | None = None,
    *,
    config_dir: str | Path = "config",
    management_config: str | Path = "/etc/eharchive/management.toml",
):
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.responses import JSONResponse, RedirectResponse
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
    templates.env.globals["css_version"] = hashlib.sha256(
        (STATIC_DIR / "v1/app.css").read_bytes()
    ).hexdigest()[:12]
    templates.env.filters["datetime"] = _format_datetime
    templates.env.filters["full_collect_datetime"] = lambda value: _format_full_collect_datetime(
        value, app_config.timezone
    )
    templates.env.filters["schedule_datetime"] = lambda value: (
        value.astimezone(ZoneInfo(app_config.timezone)).strftime("%Y-%m-%d %H:%M:%S")
        if value
        else "—"
    )
    templates.env.filters["collected_at"] = lambda value: _format_collected_at(
        value, app_config.timezone
    )
    templates.env.filters["filesize"] = _format_filesize
    templates.env.filters["status_label"] = lambda value: STATUS_LABELS.get(value, value)
    templates.env.filters["component_label"] = lambda value: COMPONENT_LABELS.get(value, value)
    templates.env.filters["safe_detail"] = safe_detail
    templates.env.filters["error_summary"] = _error_summary
    templates.env.filters["manga_tab_id"] = _manga_tab_id
    templates.env.filters["attempt_progress"] = _attempt_progress
    templates.env.filters["duration"] = _format_duration
    templates.env.filters["user_remark"] = user_remark
    templates.env.filters["special_phase_label"] = lambda value: PHASE_LABELS.get(value, value)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.database = database
    app.state.auth_enabled = auth_enabled
    app.state.config_dir = config_dir

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

    @app.post("/api/bulk-status")
    def bulk_status_update(request: Request, payload: BulkStatusUpdate):
        if (
            not request.state.auth_via_bearer
            and request.headers.get("x-csrf-token") != request.state.identity.csrf_token
        ):
            raise HTTPException(403, "CSRF validation failed")
        try:
            return bulk_override_status(
                database,
                items=payload.items,
                target_status=payload.target_status,
                reason=payload.reason,
                download_method=payload.download_method,
                superseded_by_id=payload.superseded_by_id,
                actor=_actor(request),
                app_config=app_config,
                config_dir=config_dir,
            )
        except WebServiceError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.middleware("http")
    async def authenticate(request, call_next):
        path = request.url.path
        public = path in {"/login", "/v1/login", "/health/live"} or path.startswith("/static/")
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
            login_path = "/v1/login" if path == "/v1" or path.startswith("/v1/") else "/login"
            if request.headers.get("HX-Request") == "true":
                return JSONResponse(
                    {"detail": "authentication required"},
                    status_code=401,
                    headers={"HX-Redirect": f"{login_path}?next={quote(next_path, safe='/?=&')}"},
                )
            return RedirectResponse(
                f"{login_path}?next={quote(next_path, safe='/?=&')}", status_code=303
            )
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

    @app.get("/health/live")
    def liveness():
        return {"ok": True}

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
        uploader: str | None = None,
        tags: str | None = None,
        limit: int = 100,
        page: int = 1,
    ):
        try:
            with database.session() as session:
                page = list_manga(
                    session,
                    statuses=[status] if status else None,
                    query_text=q,
                    uploader=uploader,
                    tags=tags,
                    limit=limit,
                    page=page,
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
                config_dir=config_dir,
            ),
            app_config=app_config,
        )

    control_body = Body()

    @app.put("/api/control/{component}")
    def api_control(request: Request, component: str, payload=control_body):
        from ..management.service import control_guard

        try:
            value = ControlUpdate(**payload)
            with control_guard(component, Path(management_config)), database.session() as session:
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

    from .management import register

    register(app, templates, _context, database, Path(management_config))
    from .logs import register as register_logs

    register_logs(app, templates, _context, app_config.log_dir)
    from .v1 import install_v1
    from .ui import install_ui

    install_v1(
        app,
        database=database,
        templates=templates,
        app_config=app_config,
        supervisor_config=supervisor_config,
        secrets_config=secrets_config,
        signer=signer,
        auth_enabled=auth_enabled,
        config_dir=config_dir,
        management_config=management_config,
    )
    install_ui(
        app,
        templates=templates,
        database=database,
        app_config=app_config,
        supervisor_config=supervisor_config,
        secrets_config=secrets_config,
        signer=signer,
        auth_enabled=auth_enabled,
        config_dir=config_dir,
        management_path=Path(management_config),
    )
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-web")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    migrated_files = migrate_configuration(args.config_dir)
    app_config, _, _, _ = load_config(args.config_dir)
    configure_logging(
        app_config.log_level,
        app_config.log_dir,
        timezone=app_config.timezone,
        component="web",
        run_id=str(uuid.uuid4()),
    )
    # Uvicorn's default logging configuration bypasses the application's file
    # handler. Route its lifecycle, access and exception records through root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = get_logger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(app_config.log_level.upper())
    logger = get_logger("web")
    if migrated_files:
        logger.info("配置迁移完成：%s", ", ".join(migrated_files))
    logger.info("Starting Web on %s:%s", app_config.web_host, app_config.web_port)
    import uvicorn

    try:
        application = create_app(Database(app_config.database_url), config_dir=args.config_dir)
        uvicorn.run(
            application, host=app_config.web_host, port=app_config.web_port, log_config=None
        )
    except Exception:
        logger.exception("Web startup or server failed")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
