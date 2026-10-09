"""Registered module UI routes, sharing authentication and CSRF validation."""

import json
from urllib.parse import urlencode

from fastapi import HTTPException, Request

from ....db.models import SpecialWorkflow
from ...core.service import ModuleService, SpecialInvalidRequest, SpecialServiceError
from .module import KIND


def install_routes(app, database, templates, app_config, config_dir):
    from ....web.app import (
        _actor,
        _context,
        _redirect_response,
        _special_error_response,
        _validated_form,
    )
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

    @app.post("/special/full-collect/start")
    async def start(request: Request):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                workflow = service(session, request).create(KIND, form_inputs(form))
            return _redirect_response(request, f"/special/workflows/{workflow.id}")
        except ValueError as exc:
            return error(request, exc)

    @app.get("/special/full-collect/{workflow_id}/logs")
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
            name="special/full_collect_logs.html",
            context=_context(
                request,
                **log_context(
                    database, app_config, workflow_id,
                    job_id=job_id, level=level, file=file, before=before, page=page,
                ),
            ),
        )


def log_context(database, app_config, workflow_id, *, job_id=0, level="", file="",
                before=None, page=1) -> dict:
    """Read a bounded window of one round's JSON log; shared by every interface."""
    from ....web.logs import list_logs, resolve_log

    with database.session() as session:
        workflow = session.get(SpecialWorkflow, workflow_id)
        if workflow is None or workflow.kind != KIND:
            raise HTTPException(404, "全量轮次不存在")
    if level not in {"", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} or page < 1:
        raise HTTPException(400, "日志筛选参数无效")
    query = f"_workflow-{workflow_id}_job-" + (f"{job_id}_" if job_id else "")
    try:
        available, truncated = list_logs(
            app_config.log_dir.resolve(), query, "special/full_collect"
        )
    except HTTPException:
        available, truncated = [], False
    available = [row for row in available if not row["is_directory"]]
    chosen = (
        next((row for row in available if row["name"] == file), None)
        if file
        else next(iter(available), None)
    )
    if file and chosen is None:
        raise HTTPException(404, "此工作流日志文件不存在")
    rows, previous = [], None
    if chosen:
        if before is not None and before < 0:
            raise HTTPException(400, "日志位置无效")
        path = resolve_log(app_config.log_dir, chosen["name"])
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = min(before, handle.tell()) if before is not None else handle.tell()
            start = max(0, end - 65536)
            handle.seek(start)
            if start:
                handle.readline(min(65536, end - start))
            aligned = handle.tell()
            lines = (
                handle.read(max(0, end - aligned))
                .decode("utf-8", errors="replace")
                .splitlines()
            )
            previous_offset = aligned if aligned < end else start
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and (not level or item.get("level") == level):
                rows.append(item)
        if previous_offset:
            previous = urlencode(
                {
                    "job_id": job_id,
                    "level": level,
                    "file": chosen["name"],
                    "before": previous_offset,
                }
            )
    for item in available:
        item["query"] = urlencode({"job_id": job_id, "level": level, "file": item["name"]})
    return {
        "workflow_id": workflow_id,
        "job_id": job_id,
        "level": level,
        "files": available[(page - 1) * 50 : page * 50],
        "file_page": page,
        "has_more": page * 50 < len(available),
        "truncated": truncated,
        "rows": rows,
        "selected": chosen,
        "previous": previous,
    }
