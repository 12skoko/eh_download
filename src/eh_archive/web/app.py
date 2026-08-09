from __future__ import annotations

import argparse
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import desc, func, select
from sqlalchemy.exc import SQLAlchemyError

from ..config import load_config
from ..db import Database
from ..db.models import EventLog, JobAttempt, MangaRecord, SystemControl
from ..db.repository import utcnow
from ..domain.states import Status
from ..logging import configure_logging


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
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError("Install eh-archive to use the Web process") from exc
    app_config, _, _, secrets_config = load_config(config_dir)
    database = database or Database(app_config.database_url)
    app = FastAPI(title="EH Archive", version="6.0.0")

    @app.middleware("http")
    async def require_auth(request, call_next):
        secret = secrets_config.web_secret
        if secret and request.method not in {"GET", "HEAD", "OPTIONS"}:
            authorization = request.headers.get("authorization", "")
            if authorization != f"Bearer {secret}":
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "authentication required"}, status_code=401)
        return await call_next(request)

    class ManualManga(BaseModel):
        url: str
        priority: int = Field(default=100, ge=-100000, le=100000)
        remark: str | None = None

    class RemarkUpdate(BaseModel):
        remark: str | None = None
        row_version: int

    class ControlUpdate(BaseModel):
        state: str
        reason: str | None = None
        row_version: int | None = None

    class ActionUpdate(BaseModel):
        row_version: int
        reason: str | None = None
        archive_id: str | None = None

    class ArchiveConfirmation(BaseModel):
        archive_id: str
        row_version: int
        reason: str

    @app.get(
        "/", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse
    )
    def dashboard():
        with database.session() as session:
            rows = list(
                session.scalars(
                    select(MangaRecord)
                    .order_by(desc(MangaRecord.priority), MangaRecord.created_at)
                    .limit(50)
                )
            )
        from html import escape

        body = "".join(
            f"<tr><td><a href='/api/manga/{escape(row.manga_id)}'>{escape(row.manga_id)}</a></td>"
            f"<td>{escape(row.name)}</td><td>{escape(row.status)}</td><td>{row.priority}</td></tr>"
            for row in rows
        )
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>EH Archive</title>"
            "<style>body{font:14px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}"
            "td,th{border-bottom:1px solid #ddd;padding:.5rem;text-align:left}</style></head>"
            "<body><h1>EH Archive</h1><p><a href='/health'>Health</a> | <a href='/docs'>API docs</a></p>"
            f"<table><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Priority</th></tr></thead><tbody>{body}</tbody></table>"
            "</body></html>"
        )

    @app.get("/health")
    def health():
        try:
            database.ping()
            db_ok = True
        except (SQLAlchemyError, OSError) as exc:
            db_ok = False
            error = type(exc).__name__
        controls, counts = {}, {}
        if db_ok:
            try:
                with database.session() as session:
                    controls = {
                        row.component: {"state": row.state, "heartbeat_at": row.heartbeat_at}
                        for row in session.scalars(select(SystemControl))
                    }
                    counts = dict(
                        session.execute(
                            select(MangaRecord.status, func.count()).group_by(MangaRecord.status)
                        ).all()
                    )
            except SQLAlchemyError as exc:
                db_ok = False
                error = type(exc).__name__
        storage = {}
        for location, root in app_config.roots.items():
            try:
                root.mkdir(parents=True, exist_ok=True)
                usage = shutil.disk_usage(root)
                storage[location] = {
                    "readable": root.is_dir(),
                    "writable": root.is_dir(),
                    "free_bytes": usage.free,
                    "total_bytes": usage.total,
                }
            except OSError:
                storage[location] = {"readable": False, "writable": False}
        return {
            "ok": db_ok,
            "database": db_ok,
            "error": error if not db_ok else None,
            "components": controls,
            "counts": counts,
            "storage": storage,
            "qbittorrent": {"configured": bool(app_config.qbittorrent_url)},
            "lanraragi": {"configured": bool(app_config.lanraragi_url)},
        }

    @app.get("/api/manga")
    def list_manga(status: str | None = None, limit: int = 100, offset: int = 0):
        limit = max(1, min(limit, 500))
        with database.session() as session:
            query = (
                select(MangaRecord)
                .order_by(desc(MangaRecord.priority), MangaRecord.created_at)
                .offset(offset)
                .limit(limit)
            )
            if status:
                try:
                    Status(status)
                except ValueError as exc:
                    raise HTTPException(400, "invalid status") from exc
                query = query.where(MangaRecord.status == status)
            rows = list(session.scalars(query))
            return [_serialize(row) for row in rows]

    @app.get("/api/manga/{manga_id:path}")
    def get_manga(manga_id: str):
        with database.session() as session:
            row = session.get(MangaRecord, manga_id)
            if row is None:
                raise HTTPException(404, "manga not found")
            attempts = list(
                session.scalars(
                    select(JobAttempt)
                    .where(JobAttempt.manga_id == manga_id)
                    .order_by(desc(JobAttempt.started_at))
                )
            )
            events = list(
                session.scalars(
                    select(EventLog)
                    .where(EventLog.manga_id == manga_id)
                    .order_by(desc(EventLog.created_at))
                    .limit(100)
                )
            )
            value = _serialize(row)
            value["info"] = _serialize(row.info) if row.info else None
            value["attempts"] = [
                {
                    "id": item.id,
                    "operation": item.operation,
                    "status": item.status,
                    "started_at": item.started_at,
                    "finished_at": item.finished_at,
                    "error_code": item.error_code,
                }
                for item in attempts
            ]
            value["events"] = [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "operation": event.operation,
                    "from_status": event.from_status,
                    "to_status": event.to_status,
                    "error_code": event.error_code,
                    "detail": event.detail,
                    "created_at": event.created_at,
                }
                for event in events
            ]
            return value

    @app.post("/api/manga", status_code=201)
    def add_manga(payload: ManualManga):
        manga_id = _gallery_id(payload.url)
        if manga_id is None:
            raise HTTPException(400, "URL does not contain an EH gallery id")
        with database.session() as session:
            row = session.get(MangaRecord, manga_id)
            if row is None:
                row = MangaRecord(
                    manga_id=manga_id,
                    name=manga_id,
                    link=payload.url,
                    queue_source="manual",
                    priority=payload.priority,
                    status=Status.DOWNLOAD_PENDING.value,
                    remark=payload.remark,
                )
                session.add(row)
                session.flush()
                session.add(
                    EventLog(
                        manga_id=manga_id,
                        component="web",
                        event_type="manual",
                        operation="add",
                        to_status=row.status,
                        actor="web",
                        detail={"url": payload.url},
                    )
                )
            else:
                previous = row.status
                row.priority = payload.priority
                row.remark = payload.remark
                if row.status in {
                    Status.SKIPPED.value,
                    Status.UNAVAILABLE.value,
                    Status.MANUAL_REVIEW.value,
                }:
                    row.status = Status.DOWNLOAD_PENDING.value
                    row.status_updated_at = utcnow()
                row.updated_at = utcnow()
                row.row_version += 1
                session.add(
                    EventLog(
                        manga_id=manga_id,
                        component="web",
                        event_type="manual",
                        operation="add",
                        actor="web",
                        from_status=previous,
                        to_status=row.status,
                        detail={"priority": payload.priority, "reason": payload.remark},
                    )
                )
            return _serialize(row)

    @app.patch("/api/manga/{manga_id:path}/remark")
    def update_remark(manga_id: str, payload: RemarkUpdate):
        with database.session() as session:
            row = session.get(MangaRecord, manga_id)
            if row is None:
                raise HTTPException(404, "manga not found")
            if row.row_version != payload.row_version:
                raise HTTPException(409, "row version conflict")
            row.remark = payload.remark
            row.row_version += 1
            session.add(
                EventLog(
                    manga_id=manga_id,
                    component="web",
                    event_type="manual",
                    operation="remark",
                    actor="web",
                    detail={"changed": True},
                )
            )
            return _serialize(row)

    @app.post("/api/manga/{manga_id:path}/actions/{action}")
    def action(manga_id: str, action: str, payload: ActionUpdate):
        with database.session() as session:
            row = session.get(MangaRecord, manga_id)
            if row is None:
                raise HTTPException(404, "manga not found")
            if row.row_version != payload.row_version:
                raise HTTPException(409, "row version conflict")
            event_map = {
                "retry": {
                    Status.UNAVAILABLE.value: "retry",
                    Status.SKIPPED.value: "override",
                    Status.QUARANTINED.value: "redownload",
                    Status.CANCELLED.value: "resume",
                    Status.MANUAL_REVIEW.value: "resume_download",
                },
                "skip": {Status.DISCOVERED.value: "skip"},
                "cancel": {
                    status.value: "cancel"
                    for status in (
                        Status.DISCOVERED,
                        Status.DEFERRED,
                        Status.DOWNLOAD_PENDING,
                        Status.DOWNLOADING,
                        Status.DOWNLOADED,
                        Status.VALIDATING,
                        Status.PREPARING,
                        Status.UPLOAD_PENDING,
                        Status.UPLOADED,
                        Status.SKIPPED,
                        Status.UNAVAILABLE,
                        Status.QUARANTINED,
                        Status.MANUAL_REVIEW,
                    )
                },
                "resume": {
                    Status.MANUAL_REVIEW.value: "resume_download",
                    Status.CANCELLED.value: "resume",
                },
                "validate": {
                    Status.DOWNLOADED.value: "validate",
                    Status.MANUAL_REVIEW.value: "resume_validate",
                    Status.CANCELLED.value: "resume_validate",
                },
                "upload": {
                    Status.VALIDATING.value: "upload",
                    Status.MANUAL_REVIEW.value: "resume_upload",
                    Status.CANCELLED.value: "resume_upload",
                },
                "confirm-uploaded": {Status.MANUAL_REVIEW.value: "confirm_uploaded"},
            }
            event = event_map.get(action, {}).get(row.status)
            if event is None:
                raise HTTPException(400, "unsupported action for current status")
            archive_id = None
            if action == "confirm-uploaded":
                if not payload.archive_id or not re.fullmatch(
                    r"[0-9a-fA-F]{40}", payload.archive_id
                ):
                    raise HTTPException(
                        400, "archive_id must be a 40-character SHA1 for confirmation"
                    )
                archive_id = payload.archive_id.lower()
            from ..domain.states import can_transition, transition_target

            if not can_transition(row.status, event):
                raise HTTPException(409, f"action is not valid from {row.status}")
            old = row.status
            row.status = transition_target(row.status, event).value
            if archive_id is not None:
                row.lrr_archive_id = archive_id
            row.status_updated_at = row.updated_at = utcnow()
            row.queue_source = "manual"
            row.row_version += 1
            session.add(
                EventLog(
                    manga_id=manga_id,
                    component="web",
                    event_type="manual",
                    operation=action,
                    from_status=old,
                    to_status=row.status,
                    actor="web",
                    detail={
                        **({"reason": payload.reason} if payload.reason else {}),
                        **({"archive_id": archive_id} if archive_id else {}),
                    },
                )
            )
            return _serialize(row)

    @app.post("/api/manga/{manga_id:path}/archive-confirmation")
    def confirm_archive(manga_id: str, payload: ArchiveConfirmation):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", payload.archive_id):
            raise HTTPException(400, "archive_id must be a 40-character SHA1")
        with database.session() as session:
            row = session.get(MangaRecord, manga_id)
            if row is None:
                raise HTTPException(404, "manga not found")
            if row.row_version != payload.row_version:
                raise HTTPException(409, "row version conflict")
            if row.status != Status.MANUAL_REVIEW.value:
                raise HTTPException(409, "archive confirmation requires manual_review")
            from ..domain.states import transition_target

            old = row.status
            row.lrr_archive_id = payload.archive_id.lower()
            row.status = transition_target(row.status, "confirm_uploaded").value
            row.status_updated_at = row.updated_at = utcnow()
            row.queue_source = "manual"
            row.row_version += 1
            session.add(
                EventLog(
                    manga_id=manga_id,
                    component="web",
                    event_type="manual",
                    operation="confirm_uploaded",
                    from_status=old,
                    to_status=row.status,
                    actor="web",
                    detail={"reason": payload.reason, "archive_id": payload.archive_id.lower()},
                )
            )
            return _serialize(row)

    @app.put("/api/control/{component}")
    def control(component: str, payload: ControlUpdate):
        if payload.state not in {"running", "paused"}:
            raise HTTPException(400, "state must be running or paused")
        with database.session() as session:
            row = session.get(SystemControl, component)
            if row is None:
                row = SystemControl(
                    component=component,
                    state=payload.state,
                    reason=payload.reason,
                    updated_by="web",
                )
                session.add(row)
            else:
                if payload.row_version is not None and row.row_version != payload.row_version:
                    raise HTTPException(409, "row version conflict")
                row.state, row.reason, row.updated_by, row.row_version = (
                    payload.state,
                    payload.reason,
                    "web",
                    row.row_version + 1,
                )
            session.add(
                EventLog(
                    component="web",
                    event_type="manual",
                    operation="control",
                    actor="web",
                    detail={
                        "component": component,
                        "state": payload.state,
                        "reason": payload.reason,
                    },
                )
            )
            return {
                "component": row.component,
                "state": row.state,
                "reason": row.reason,
                "row_version": row.row_version,
            }

    return app


def _serialize(row):
    if row is None:
        return None
    if hasattr(row, "manga_id"):
        return {
            key: getattr(row, key)
            for key in (
                "manga_id",
                "name",
                "real_name",
                "link",
                "torrent_link",
                "posted_at",
                "category",
                "tags_raw",
                "pages",
                "rating",
                "uploader",
                "remark",
                "queue_source",
                "status",
                "screen_pending",
                "screen_group_id",
                "priority",
                "download_method",
                "defer_until",
                "attempt_count",
                "next_retry_at",
                "external_download_id",
                "artifact_location",
                "artifact_filename",
                "artifact_kind",
                "artifact_generation",
                "artifact_size",
                "artifact_sha1",
                "artifact_checked_at",
                "lrr_archive_id",
                "row_version",
                "created_at",
                "updated_at",
            )
            if hasattr(row, key)
        }
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


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
