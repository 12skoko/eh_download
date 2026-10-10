"""V1 full-collection forms and log page."""

from fastapi import Request
from ...special.core.service import ModuleService, SpecialInvalidRequest, SpecialServiceError
from ...special.modules.full_collect.module import KIND
from ...special.modules.full_collect.routes import log_context


def install_routes(app, database, templates, app_config, config_dir):
    from ..shared import (
        _actor,
        _context,
        _redirect_response,
        _validated_form,
    )
    from .common import _special_error_response

    def service(session, request):
        return ModuleService(
            session, actor=_actor(request), config_dir=config_dir, app_config=app_config
        )

    def form_inputs(form):
        return {
            name: str(form[name]).strip()
            for name in ("base_url", "account", "start_id", "end_id")
            if form.get(name)
        }

    def error(request, exc):
        return _special_error_response(
            request,
            templates,
            exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc)),
        )

    @app.post("/v1/special/full-collect/start")
    async def start(request: Request):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                workflow = service(session, request).create(KIND, form_inputs(form))
            return _redirect_response(request, f"/v1/special/workflows/{workflow.id}")
        except ValueError as exc:
            return error(request, exc)

    @app.get("/v1/special/full-collect/{workflow_id}/logs")
    def logs(
        request: Request,
        workflow_id: int,
        job_id: int = 0,
        level: str = "",
        file: str = "",
        before: int | None = None,
        page: int = 1,
    ):
        return templates.TemplateResponse(
            request=request,
            name="v1/special/full_collect_logs.html",
            context=_context(
                request,
                **log_context(
                    database,
                    app_config,
                    workflow_id,
                    job_id=job_id,
                    level=level,
                    file=file,
                    before=before,
                    page=page,
                ),
            ),
        )
