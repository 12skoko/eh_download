import json
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from ..special.core.outputs import output_path
from ..special.core.service import (
    ModuleService,
    SpecialInvalidRequest,
    SpecialServiceError,
)


def install_special_routes(app, database, templates, app_config, config_dir):
    from ..special.catalog import MODULES, load_modules
    from .app import _actor, _context, _redirect_response, _special_error_response, _validated_form

    load_modules()
    for module in MODULES.values():
        if module.install_routes:
            module.install_routes(app, database, templates, app_config, config_dir)

    def service(session, request):
        return ModuleService(
            session, actor=_actor(request), config_dir=config_dir, app_config=app_config
        )

    @app.post("/special/modules/{kind}/create")
    async def create(request: Request, kind: str):
        form = await _validated_form(request)
        try:
            inputs = json.loads(str(form.get("inputs", "{}")))
            if not isinstance(inputs, dict):
                raise SpecialInvalidRequest("输入必须是对象")
            with database.session() as session:
                workflow = service(session, request).create(kind, inputs)
            return _redirect_response(request, f"/special/workflows/{workflow.id}")
        except ValueError as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc))
            return _special_error_response(request, templates, error)

    @app.post("/special/workflows/{workflow_id}/actions/{action}")
    async def action(request: Request, workflow_id: int, action: str):
        form = await _validated_form(request)
        try:
            inputs = json.loads(str(form.get("inputs", "{}")))
            if action == "release-expired" and "reason" in form:
                inputs = {
                    "reason": str(form.get("reason", "")),
                    "confirmed": form.get("confirmed") == "yes",
                }
            if not isinstance(inputs, dict):
                raise SpecialInvalidRequest("输入必须是对象")
            with database.session() as session:
                service(session, request).action(
                    workflow_id,
                    action,
                    row_version=int(str(form.get("row_version", ""))),
                    inputs=inputs,
                )
            return _redirect_response(request, f"/special/workflows/{workflow_id}")
        except ValueError as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc))
            return _special_error_response(request, templates, error)

    def resolve_output(workflow_id, output_id):
        from ..special.service import special_workflow_detail

        try:
            with database.session() as session:
                detail = special_workflow_detail(session, workflow_id)
        except SpecialServiceError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        entry = next(
            (e for e in detail["payload"].get("outputs", []) if e["id"] == output_id), None
        )
        if not entry:
            raise HTTPException(404, "输出不存在")
        try:
            path = output_path(Path(app_config.log_dir) / "special_outputs", entry["storage_key"])
        except ValueError:
            raise HTTPException(404, "输出引用无效") from None
        if not path.is_file():
            raise HTTPException(404, "报告文件已丢失，当前输出不可用")
        return detail, entry, path

    @app.get("/special/workflows/{workflow_id}/outputs/{output_id}/download")
    def download(workflow_id: int, output_id: str):
        _, entry, path = resolve_output(workflow_id, output_id)
        return FileResponse(path, media_type=entry["media_type"], filename=Path(entry["name"]).name)

    @app.get("/special/workflows/{workflow_id}/outputs/{output_id}")
    def report(
        request: Request,
        workflow_id: int,
        output_id: str,
        section: str = "database_only",
        page: int = 1,
    ):
        detail, entry, path = resolve_output(workflow_id, output_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(409, "报告文件损坏或不可读取") from None
        if not isinstance(data, dict):
            raise HTTPException(409, "报告结构无效")
        sections = {
            key: value
            for key, value in data.items()
            if isinstance(value, (list, dict)) and key != "summary"
        }
        if prepare_sections := detail.get("report_sections"):
            sections, section = prepare_sections(sections, section)
        if section not in sections:
            raise HTTPException(400, "未知报告分组")
        rows = sections[section]
        rows = (
            [{"id": key, "count": value} for key, value in rows.items()]
            if isinstance(rows, dict)
            else rows
        )
        page = max(1, page)
        page_rows = rows[(page - 1) * 100 : page * 100]
        if presenter := detail.get("report_presenter"):
            with database.session() as session:
                page_rows = presenter(session, section, page_rows)
        else:
            page_rows = [row if isinstance(row, dict) else {"id": row} for row in page_rows]
        return templates.TemplateResponse(
            request=request,
            name="special/report.html",
            context=_context(
                request,
                **detail,
                output=entry,
                section=section,
                sections={key: len(value) for key, value in sections.items()},
                report_summary=data.get("summary", {}),
                report_rows=page_rows,
                report_page=page,
                report_total=len(rows),
            ),
        )
