import json
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse

from ...special.core.service import ModuleService, SpecialInvalidRequest, SpecialServiceError


from ..special_routes import resolve_output, report_context


def install_special_routes(app, database, templates, app_config, config_dir):
    from ...special.catalog import MODULES, load_modules
    from ..shared import _actor, _context, _redirect_response, _validated_form
    from .common import _special_error_response

    load_modules()
    for module in MODULES.values():
        if module.install_routes:
            module.install_routes(app, database, templates, app_config, config_dir)

    def service(session, request):
        return ModuleService(
            session, actor=_actor(request), config_dir=config_dir, app_config=app_config
        )

    @app.post("/v1/special/modules/{kind}/create")
    async def create(request: Request, kind: str):
        form = await _validated_form(request)
        try:
            inputs = json.loads(str(form.get("inputs", "{}")))
            if not isinstance(inputs, dict):
                raise SpecialInvalidRequest("输入必须是对象")
            with database.session() as session:
                workflow = service(session, request).create(kind, inputs)
            return _redirect_response(request, f"/v1/special/workflows/{workflow.id}")
        except ValueError as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc))
            return _special_error_response(request, templates, error)

    @app.post("/v1/special/workflows/{workflow_id}/actions/{action}")
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
                service(session, request).action(
                    workflow_id,
                    action,
                    row_version=int(str(form.get("row_version", ""))),
                    inputs=inputs,
                )
            return _redirect_response(request, f"/v1/special/workflows/{workflow_id}")
        except ValueError as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc))
            return _special_error_response(request, templates, error)

    @app.get("/v1/special/workflows/{workflow_id}/outputs/{output_id}/download")
    def download(workflow_id: int, output_id: str):
        _, entry, path = resolve_output(database, app_config, workflow_id, output_id)
        return FileResponse(path, media_type=entry["media_type"], filename=Path(entry["name"]).name)

    @app.get("/v1/special/workflows/{workflow_id}/outputs/{output_id}")
    def report(
        request: Request,
        workflow_id: int,
        output_id: str,
        section: str = "database_only",
        page: int = 1,
    ):
        values = report_context(database, app_config, workflow_id, output_id, section, page)
        return templates.TemplateResponse(
            request=request,
            name="v1/" + values.get("report_template", "special/report.html"),
            context=_context(request, **values),
        )
