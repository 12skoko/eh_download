from __future__ import annotations


from ...special.service import (
    SpecialConflict,
    SpecialInvalidRequest,
    SpecialNotFound,
    SpecialServiceError,
    SpecialWorkflowService,
)
from ..services import Conflict, InvalidRequest, WebService, WebServiceError

from ..shared import _context, _actor, _redirect_response


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
    return _redirect_response(request, f"/v1/manga/{manga_id}?notice={notice}")


def _special_page_update(
    request,
    templates,
    database,
    workflow_id,
    callback,
    *,
    config_dir,
    app_config,
    notice,
):
    try:
        with database.session() as session:
            callback(
                SpecialWorkflowService(
                    session,
                    actor=_actor(request),
                    config_dir=config_dir,
                    app_config=app_config,
                )
            )
    except (ValueError, SpecialServiceError) as exc:
        error = (
            exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest("表单数据无效")
        )
        return _special_error_response(request, templates, error)
    return _redirect_response(request, f"/v1/special/workflows/{workflow_id}?notice={notice}")


def _special_error_response(request, templates, exc: SpecialServiceError):
    if isinstance(exc, SpecialNotFound):
        converted: WebServiceError = WebServiceError(str(exc))
        converted.status_code = 404
    elif isinstance(exc, SpecialConflict):
        converted = Conflict(str(exc))
    else:
        converted = InvalidRequest(str(exc))
    return _error_response(request, templates, converted)


def _error_response(request, templates, exc: WebServiceError):
    return templates.TemplateResponse(
        request=request,
        name="v1/error.html",
        context=_context(request, message=str(exc), status_code=exc.status_code),
        status_code=exc.status_code,
    )
