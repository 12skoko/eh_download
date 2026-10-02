"""Reuse legacy request handlers, keeping v2 presentation and redirects local.

Handlers retain their FastAPI signatures, CSRF checks, service calls and error
status codes. Only template selection and navigation change here.
"""
from __future__ import annotations

import inspect
import json
from typing import get_type_hints
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from ...management import ManagementError
from .common import PREFIX, is_partial


def location(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return value
    path = parts.path
    for old, new in (
        ("/special/full-collect/", PREFIX + "/full-collect/"),
        ("/special/manual-torrent/", PREFIX + "/manual-torrent/"),
        ("/special/video-archive/", PREFIX + "/video-archive/"),
        ("/special/modules/", PREFIX + "/workflows/"),
        ("/special/workflows/", PREFIX + "/workflow/"),
        ("/config/", PREFIX + "/settings/"),
        ("/system/", PREFIX + "/system/"),
    ):
        if path.startswith(old):
            path = new + path[len(old):]
            break
    else:
        path = {"/special": PREFIX + "/workflows", "/config": PREFIX + "/settings",
                "/logs": PREFIX + "/logs", "/logs/view": PREFIX + "/logs/view",
                "/events": PREFIX + "/events"}.get(path, path)
    return urlunsplit(("", "", path, parts.query, parts.fragment))


def mount(app, ctx, source: str, target: str, *, template=None, partial=None):
    """Mount an existing HTML/form handler with its original parameter validation."""
    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == source)
    original = route.endpoint

    async def adapted(**kwargs):
        request = kwargs.get("request")
        original_kwargs = {key: value for key, value in kwargs.items()
                           if key in signature.parameters}
        try:
            response = (await original(**original_kwargs) if inspect.iscoroutinefunction(original)
                        else await run_in_threadpool(original, **original_kwargs))
        except HTTPException as exc:
            return ctx.error(request, str(exc.detail), exc.status_code)
        except ManagementError as exc:
            code = {"not_found": 404, "operation_conflict": 409, "invalid_request": 422,
                    "not_installed": 503, "unsupported_platform": 503}.get(exc.code, 400)
            return ctx.error(request, str(exc), code)
        if hasattr(response, "context"):
            values = dict(response.context)
            values.pop("request", None)
            if ("kind" in request.path_params and "workflow" in values
                    and request.path_params["kind"] != values["workflow"].kind):
                return ctx.error(request, "工作流不属于此模块", 404)
            if response.status_code >= 400 and "message" in values:
                return ctx.error(request, values["message"], response.status_code)
            values.pop("status_code", None)
            name = partial if partial and is_partial(request) else template
            if name is None:
                raise RuntimeError(f"Missing v2 template for {source}")
            render = ctx.partial if name == partial else ctx.page
            return render(request, name, status_code=response.status_code,
                          headers={k: v for k, v in response.headers.items()
                                   if k not in {"content-length", "content-type"}}, **values)
        if isinstance(response, JSONResponse):
            data = json.loads(response.body)
            if isinstance(data, dict) and "redirect" in data:
                data["redirect"] = location(data["redirect"])
                return JSONResponse(data, status_code=response.status_code)
        for header in ("location", "hx-redirect"):
            if header in response.headers:
                response.headers[header] = location(response.headers[header])
        return response

    hints = get_type_hints(original)
    signature = inspect.signature(original)
    parameters = [
        p.replace(annotation=hints.get(p.name, p.annotation))
        for p in signature.parameters.values()
    ]
    if "request" not in signature.parameters:
        from fastapi import Request

        parameters.append(inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY,
                                            annotation=Request))
    adapted.__signature__ = signature.replace(parameters=parameters, return_annotation=signature.empty)
    adapted.__name__ = "uiv2_" + original.__name__
    app.add_api_route(target, adapted, methods=sorted(route.methods),
                      response_class=route.response_class, include_in_schema=False)
