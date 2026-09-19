from fastapi import Request

from ...core.service import ModuleService, SpecialInvalidRequest, SpecialServiceError
from .module import KIND


def install_routes(app, database, templates, app_config, config_dir):
    from ....web.app import _actor, _redirect_response, _special_error_response, _validated_form

    @app.post("/special/manual-torrent/start/{manga_id:path}")
    async def start(request: Request, manga_id: str):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                workflow = ModuleService(
                    session, actor=_actor(request), config_dir=config_dir, app_config=app_config
                ).create(
                    KIND,
                    {
                        "manga_id": manga_id,
                        "row_version": int(str(form.get("row_version", ""))),
                    },
                )
            return _redirect_response(request, f"/special/workflows/{workflow.id}")
        except ValueError as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc))
            return _special_error_response(request, templates, error)

    @app.post("/special/manual-torrent/{workflow_id}/choose")
    async def choose(request: Request, workflow_id: int):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                ModuleService(
                    session, actor=_actor(request), config_dir=config_dir, app_config=app_config
                ).action(
                    workflow_id,
                    "choose",
                    row_version=int(str(form.get("row_version", ""))),
                    inputs={
                        "choice_id": str(form.get("choice_id", "")),
                        "accepted_warnings": list(form.getlist("accepted_warnings")),
                        "allow_personalized": form.get("allow_personalized") == "yes",
                    },
                )
            return _redirect_response(request, f"/special/workflows/{workflow_id}")
        except ValueError as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc))
            return _special_error_response(request, templates, error)
