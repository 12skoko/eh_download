"""Form adapter only; CLI and Web execute the same module commands."""

import re

from fastapi import Request

from ...core.service import ModuleService, SpecialInvalidRequest, SpecialServiceError


def install_routes(app, database, templates, app_config, config_dir):
    from ....web.app import _actor, _redirect_response, _special_error_response, _validated_form

    @app.post("/special/modules/lanraragi_metadata/start")
    async def start(request: Request):
        form = await _validated_form(request)
        try:
            mode = str(form.get("mode", "selected"))
            if mode not in {"selected", "mismatch"}:
                raise SpecialInvalidRequest("未知选择方式")
            inputs = (
                {"mismatch_only": True}
                if mode == "mismatch"
                else {
                    "manga_ids": [
                        v for v in re.split(r"[\s,，]+", str(form.get("manga_ids", ""))) if v
                    ]
                }
            )
            with database.session() as session:
                workflow = ModuleService(
                    session, actor=_actor(request), config_dir=config_dir, app_config=app_config
                ).create("lanraragi_metadata", inputs)
            return _redirect_response(request, f"/special/workflows/{workflow.id}")
        except ValueError as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc))
            return _special_error_response(request, templates, error)
