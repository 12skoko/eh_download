import json
from pathlib import Path

from fastapi import HTTPException

from ..special.core.outputs import output_path
from ..special.core.service import SpecialServiceError


def resolve_output(database, app_config, workflow_id, output_id):
    """Locate a published workflow output; shared by every interface."""
    from ..special.service import special_workflow_detail

    try:
        with database.session() as session:
            detail = special_workflow_detail(session, workflow_id)
    except SpecialServiceError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    entry = next((e for e in detail["payload"].get("outputs", []) if e["id"] == output_id), None)
    if not entry:
        raise HTTPException(404, "输出不存在")
    try:
        path = output_path(Path(app_config.log_dir) / "special_outputs", entry["storage_key"])
    except ValueError:
        raise HTTPException(404, "输出引用无效") from None
    if not path.is_file():
        raise HTTPException(404, "报告文件已丢失，当前输出不可用")
    return detail, entry, path


def report_context(database, app_config, workflow_id, output_id, section, page) -> dict:
    """Read one page of a report output, applying the module's grouping and presenter."""
    detail, entry, path = resolve_output(database, app_config, workflow_id, output_id)
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
    return {
        **detail,
        "output": entry,
        "section": section,
        "sections": {key: len(value) for key, value in sections.items()},
        "report_summary": data.get("summary", {}),
        "report_rows": page_rows,
        "report_page": page,
        "report_total": len(rows),
    }
