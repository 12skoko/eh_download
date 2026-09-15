from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from ..management import ManagementError
from ..management.config import (
    DEFAULT_CONFIG,
    SUPERVISOR_UNIT,
    WEB_UNIT,
    load_management_config,
)
from ..management.git import GitRepository
from ..management.service import cancel, submit
from ..management.state import OperationStore, read_json, tail
from ..management.systemd import Systemd
from ..management.update_check import check_updates, saved_check


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "restart_web",
        "restart_supervisor",
        "restart_all",
        "git_update",
        "start_web",
        "start_supervisor",
        "start_all",
        "stop_web",
        "stop_supervisor",
        "stop_all",
    ]


def register(app, templates, context, database, management_path: Path = DEFAULT_CONFIG):
    def config():
        return load_management_config(management_path)

    def store():
        return OperationStore(config())

    def detail(identifier):
        path = store().locate(identifier)
        result = store().observed(read_json(path / "state.json"), Systemd())
        result["log"] = tail(path / "operation.log")
        result["events"] = tail(path / "events.jsonl")
        if (path / "configuration.json").exists():
            result["configuration"] = read_json(path / "configuration.json")
        return result

    @app.exception_handler(ManagementError)
    async def management_error(request, exc):
        status = {
            "not_found": 404,
            "operation_conflict": 409,
            "invalid_request": 422,
            "not_installed": 503,
            "unsupported_platform": 503,
        }.get(exc.code, 400)
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=status)

    @app.get("/system", response_class=HTMLResponse)
    def system_page(request: Request):
        error = None
        try:
            config()
        except ManagementError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request=request, name="system.html", context=context(request, management_error=error)
        )

    @app.get("/system/operations/{identifier}", response_class=HTMLResponse)
    def operation_page(request: Request, identifier: str):
        return templates.TemplateResponse(
            request=request,
            name="system_operation.html",
            context=context(request, operation=detail(identifier)),
        )

    @app.get("/api/system/status")
    def status():
        config()
        systemd = Systemd()
        result = {
            "services": {unit: systemd.status(unit) for unit in (WEB_UNIT, SUPERVISOR_UNIT)},
            "operations": [store().observed(state, systemd) for state in store().list()],
        }
        try:
            from ..db.models import SystemControl
            from .services import running_attempts, running_module_tasks, serialize_model

            with database.session() as session:
                control = session.get(SystemControl, "supervisor")
                result["control"] = serialize_model(control) if control else None
                result["workers"] = [serialize_model(row) for row in running_attempts(session)]
                result["modules"] = running_module_tasks(session)
        except SQLAlchemyError:
            result["database_error"] = "Supervisor status unavailable"
        return result

    @app.get("/api/system/git")
    def git_status():
        current = config()
        return {
            **GitRepository(current).inspect(),
            "check": saved_check(current),
            "schedule": {
                "enabled": current.update_check_enabled,
                "time": current.update_check_time,
            },
        }

    @app.post("/api/system/git/fetch")
    def git_fetch():
        return check_updates(config())

    @app.get("/api/system/operations")
    def operations():
        current = store()
        return [current.observed(state, Systemd()) for state in current.list()]

    @app.post("/api/system/operations", status_code=202)
    def create_operation(request: Request, payload: OperationRequest):
        return submit(
            payload.kind, f"web:{request.state.identity.username}", management_path=management_path
        )

    @app.get("/api/system/operations/{identifier}")
    def get_operation(identifier: str):
        return detail(identifier)

    @app.post("/api/system/operations/{identifier}/cancel", status_code=202)
    def cancel_operation(identifier: str):
        return cancel(identifier, management_path=management_path)
