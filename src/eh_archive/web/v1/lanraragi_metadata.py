"""Form adapter only; CLI and Web execute the same module commands."""

import re

from fastapi import Request
from fastapi.responses import JSONResponse

from ...special.core.service import ModuleService, SpecialInvalidRequest, SpecialServiceError
from ...special.modules.lanraragi_metadata.config import load_metadata_config
from ...special.modules.lanraragi_metadata.module import pending_mismatch_ids


def install_routes(app, database, templates, app_config, config_dir):
    from ..shared import _actor, _redirect_response, _validated_form
    from .common import _special_error_response

    @app.get("/v1/special/modules/lanraragi_metadata/mismatch-ids")
    def mismatch_ids():
        with database.session() as session:
            ids = pending_mismatch_ids(session)
        return JSONResponse(
            {"manga_ids": ids, "batch_limit": load_metadata_config(config_dir).batch_limit},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/special/modules/lanraragi_metadata/start")
    async def start(request: Request):
        form = await _validated_form(request)
        try:
            inputs = {
                "manga_ids": [v for v in re.split(r"[\s,，]+", str(form.get("manga_ids", ""))) if v]
            }
            if str(form.get("archive_id", "")).strip():
                inputs["archive_id"] = str(form["archive_id"]).strip()
            with database.session() as session:
                workflow = ModuleService(
                    session, actor=_actor(request), config_dir=config_dir, app_config=app_config
                ).create("lanraragi_metadata", inputs)
            return _redirect_response(request, f"/v1/special/workflows/{workflow.id}")
        except ValueError as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc))
            return _special_error_response(request, templates, error)
