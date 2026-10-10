from __future__ import annotations


from fastapi import Request
from fastapi.responses import HTMLResponse

from ...management import ManagementError
from ...management.config import load_management_config
from ...management.state import OperationStore, read_json, tail
from ...management.systemd import Systemd


def register(app, templates, context, database, management_path):
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

    @app.get("/v1/system", response_class=HTMLResponse)
    def system_page(request: Request):
        error = None
        try:
            config()
        except ManagementError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request=request, name="v1/system.html", context=context(request, management_error=error)
        )

    @app.get("/v1/system/operations/{identifier}", response_class=HTMLResponse)
    def operation_page(request: Request, identifier: str):
        return templates.TemplateResponse(
            request=request,
            name="v1/system_operation.html",
            context=context(request, operation=detail(identifier)),
        )
