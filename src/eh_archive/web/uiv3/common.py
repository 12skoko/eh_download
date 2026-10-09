"""Shared helpers for the /uiv3 interface.

The v3 layer only changes presentation and navigation. Every write goes through
the same service methods, validation, CSRF and row_version checks as the
original pages; responses differ only in where the user lands afterwards.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from ...db.models import MangaRecord, SystemControl

PREFIX = "/uiv3"
STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "uiv3"

# 流水线分组：(键, 标题, 状态, 所属模块)。24 个状态全部出现且只出现一次。
# 模块只挂在主流程上；旁路分组只展示档案数量，特殊处理与档案删除归入“完成”。
# 这两组只决定概览的排布，每组第一个状态在概览中显示为大字。
PIPELINE = (
    ("collect", "采集与筛选",
     ("discovered", "deferred", "unavailable", "filtered_out", "skipped"), ("collect", "screen")),
    ("download", "下载", ("download_pending", "downloading", "download_blocked"),
     ("details", "torrent_download", "torrent_check", "direct_download")),
    ("verify", "校验与打包", ("downloaded", "rename_pending", "validating", "preparing"),
     ("validate", "prepare")),
    ("upload", "上传", ("upload_pending", "uploading", "uploaded"), ("upload", "cleanup")),
    ("done", "完成", ("completed", "deleted"), ("special_processing", "delete")),
)
SIDE_GROUPS = (
    ("exceptions", "异常与旁路", "triangle-alert",
     ("manual_review", "quarantined", "special_processing", "cancel_requested", "cancelled"), ()),
    ("deletion", "删除队列", "trash-2", ("outdated", "force_delete_pending"), ()),
)
# 档案页筛选弹层的分组独立于概览排布，保持原有分组。
STATUS_GROUPS = (
    ("采集与筛选", ("deferred", "discovered")),
    ("下载", ("download_pending", "downloading", "download_blocked")),
    ("校验与打包", ("downloaded", "rename_pending", "validating", "preparing")),
    ("上传", ("upload_pending", "uploading", "uploaded")),
    ("完成", ("completed",)),
    ("异常与旁路", ("manual_review", "quarantined", "special_processing", "unavailable",
               "cancel_requested", "cancelled")),
    ("删除与未收录", ("outdated", "force_delete_pending", "deleted", "filtered_out", "skipped")),
)
MODULE_ICONS = {
    "collect": "radio", "screen": "filter", "details": "file-search",
    "torrent_download": "magnet", "torrent_check": "timer", "direct_download": "download",
    "validate": "package-check", "prepare": "file-archive", "upload": "cloud-upload",
    "cleanup": "eraser", "delete": "trash-2", "special_processing": "workflow",
}
MODULE_KIND_ICONS = {
    "video_archive": "film", "manual_torrent": "magnet", "lanraragi_metadata": "tag",
    "download_cleanup": "eraser", "lanraragi_compare": "git-compare", "full_collect": "layers",
}

# 档案状态的唯一配色映射，24 个状态各出现一次。胶囊、流程条、概览计数、
# 筛选、状态对话框和批量下拉都只从这里取色；运行状态另用 _RUN_TONES。
_STATUS_TONES = {
    # 流程中
    "downloading": "run", "validating": "run", "preparing": "run", "uploading": "run",
    # 特殊处理
    "special_processing": "special",
    # 等待流程
    "deferred": "warn", "discovered": "warn", "download_pending": "warn", "downloaded": "warn",
    "rename_pending": "warn", "upload_pending": "warn", "uploaded": "warn",
    "cancel_requested": "warn", "outdated": "warn", "force_delete_pending": "warn",
    # 完成
    "completed": "ok",
    # 需要人处理
    "manual_review": "danger", "quarantined": "danger",
    # 已出库或不再推进
    "unavailable": "muted", "deleted": "muted", "filtered_out": "muted", "skipped": "muted",
    "download_blocked": "muted", "cancelled": "muted",
}
_RUN_TONES = {
    "queued": "idle", "running": "run", "succeeded": "ok", "completed": "ok", "failed": "danger",
    "cancelled": "muted", "active": "special", "pending": "idle", "interrupted": "danger",
    "healthy": "ok", "degraded": "warn", "stale": "muted", "unknown": "muted", "error": "danger",
    "unhealthy": "danger", "unavailable": "danger", "paused": "muted", "draining": "warn",
}
RUN_LABELS = {
    "queued": "排队中", "running": "运行中", "succeeded": "成功", "failed": "失败",
    "cancelled": "已取消", "active": "进行中", "completed": "已完成", "pending": "待执行",
    "healthy": "正常", "degraded": "降级", "stale": "过期", "unknown": "未知", "error": "异常",
    "unhealthy": "异常", "unavailable": "不可用", "paused": "已暂停", "draining": "排空中",
}
TOASTS = {
    "remark-updated": "备注已保存", "priority-updated": "优先级已更新",
    "video-skipped": "已跳过视频并返回普通流程", "action-completed": "操作已提交",
    "expired-lease-released": "已解除过期租约并转入人工复核",
    "conflict-versions-resolved": "已登记版本替代", "conflict-rename-requested": "已登记冲突改名",
    "status-updated": "状态已修改", "torrent-link-permission-saved": "备用链接授权已保存",
    "torrent-warning-updated": "种子告警授权已更新", "workflow-created": "工作流已创建",
    "workflow-updated": "操作已提交，Supervisor 将领取对应任务",
    "control-updated": "调度控制已更新", "logged-in": "已登录",
}


def tone(status: str | None) -> str:
    return _STATUS_TONES.get(status or "", "idle")


def run_tone(status: str | None) -> str:
    return _RUN_TONES.get(status or "", "idle")


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def ago(value: Any) -> str:
    """Compact relative time used in lists; exact time stays in the title attribute."""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    moment = aware(value)
    seconds = (datetime.now(UTC) - moment).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 45:
        text = "刚刚" if not future else "即将"
        return text
    if seconds < 3600:
        amount = f"{int(seconds // 60) or 1} 分钟"
    elif seconds < 86400:
        amount = f"{int(seconds // 3600)} 小时"
    elif seconds < 86400 * 7:
        amount = f"{int(seconds // 86400)} 天"
    else:
        return moment.astimezone().strftime("%Y-%m-%d")
    return amount + ("后" if future else "前")


def clock(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    moment = aware(value).astimezone()
    today = datetime.now(UTC).astimezone().date()
    return moment.strftime("%H:%M" if moment.date() == today else "%m-%d %H:%M")


def asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("uiv3.css", "uiv3.js", "uiv3-pages.js"):
        path = STATIC_DIR / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def is_partial(request, target: str | None = None) -> bool:
    """A non-boosted HTMX request, optionally aimed at a specific element id."""
    if not is_htmx(request) or request.headers.get("HX-Boosted") == "true":
        return False
    return target is None or request.headers.get("HX-Target") == target


def trigger_header(*, toast: str | None = None, tone_name: str = "ok", **events) -> dict:
    """Toasts fire on receipt; other events fire after the swap so handlers see new DOM."""
    headers: dict[str, str] = {}
    if toast:
        # ensure_ascii keeps the header latin-1 safe; htmx decodes the escapes.
        headers["HX-Trigger"] = json.dumps(
            {"uiv3:toast": {"message": toast, "tone": tone_name}}, ensure_ascii=True
        )
    after = {"uiv3:" + name.replace("_", "-"): value for name, value in events.items()}
    if after:
        headers["HX-Trigger-After-Swap"] = json.dumps(after, ensure_ascii=True)
    return headers


def redirect(request, location: str, toast_key: str | None = None) -> Response:
    if toast_key:
        location += ("&" if "?" in location else "?") + urlencode({"toast": toast_key})
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": location})
    return RedirectResponse(location, status_code=303)


def old_url(request) -> str:
    """Equivalent page in the original interface, used for side-by-side comparison."""
    path = request.url.path[len(PREFIX):] or "/"
    query = request.url.query
    parts = [part for part in path.split("/") if part]
    mapping = {"archives": "/manga", "inbox": "/review", "events": "/events", "logs": "/logs",
               "settings": "/config", "system": "/system", "workflows": "/special"}
    target = "/"
    if parts:
        head = parts[0]
        if head == "archives" and len(parts) > 1:
            target = "/manga/" + "/".join(parts[1:])
        elif head == "workflows" and len(parts) == 2:
            target = f"/special/modules/{parts[1]}"
        elif head == "workflows" and len(parts) >= 3:
            target = f"/special/workflows/{parts[2]}"
        elif head == "workflow" and len(parts) >= 2:
            rest = "/".join(parts[2:])
            if rest == "logs":
                target = f"/special/full-collect/{parts[1]}/logs"
            else:
                target = f"/special/workflows/{parts[1]}" + (f"/{rest}" if rest else "")
        elif head == "logs" and parts[1:] == ["view"]:
            target = "/logs/view"
        elif head == "system" and len(parts) > 1:
            target = "/system/" + "/".join(parts[1:])
        elif head == "pane" and len(parts) > 1:
            target = "/manga/" + "/".join(parts[1:])
        else:
            target = mapping.get(head, "/")
    return target + (f"?{query}" if query and "toast=" not in query else "")


def manga_url(manga_id: str) -> str:
    return f"{PREFIX}/archives/{quote(manga_id, safe='/')}"


_TRACK_STAGES = ("采集", "下载", "校验", "上传", "完成")
_STAGE_OF_STATUS = {
    "deferred": 0, "discovered": 0,
    "download_pending": 1, "downloading": 1, "download_blocked": 1,
    "downloaded": 2, "rename_pending": 2, "validating": 2, "preparing": 2,
    "upload_pending": 3, "uploading": 3, "uploaded": 3, "completed": 4,
}
_STAGE_OF_OPERATION = {
    "collect": 0, "screen": 0, "details": 1, "torrent_download": 1, "torrent_check": 1,
    "direct_download": 1, "validate": 2, "prepare": 2, "upload": 3, "cleanup": 3,
}


def track(row) -> dict | None:
    """Where an archive sits in the pipeline; None for states outside the main flow."""
    status = row.status
    held = status in {"manual_review", "quarantined", "special_processing", "cancel_requested", "cancelled"}
    if status in _STAGE_OF_STATUS:
        index = _STAGE_OF_STATUS[status]
    elif held:
        index = _STAGE_OF_OPERATION.get(row.last_error_operation or "", 1)
    else:
        return None
    label = _TRACK_STAGES[index] + (" · 暂停于此" if held else "")
    return {"stages": _TRACK_STAGES, "index": index, "tone": tone(status), "label": label}


def phase_labels_by_kind() -> dict[str, dict[str, str]]:
    from ...special.modules.download_cleanup.module import PHASE_LABELS as cleanup
    from ...special.modules.full_collect.module import PHASE_LABELS as full
    from ...special.modules.lanraragi_compare.module import PHASE_LABELS as compare
    from ...special.modules.lanraragi_metadata.module import PHASE_LABELS as metadata
    from ...special.modules.manual_torrent.module import PHASES as manual
    from ...special.remarks import PHASE_LABELS as video

    return {"video_archive": video, "manual_torrent": manual, "lanraragi_metadata": metadata,
            "download_cleanup": cleanup, "full_collect": full, "lanraragi_compare": compare}


@dataclass
class Uiv3:
    templates: Any
    database: Any
    app_config: Any
    supervisor_config: Any
    secrets_config: Any
    signer: Any
    auth_enabled: bool
    config_dir: Path
    management_path: Path

    def shell(self, request) -> dict[str, Any]:
        from ..app import _supervisor_status

        values: dict[str, Any] = {
            "v3_old_url": old_url(request),
            "v3_toast": TOASTS.get(request.query_params.get("toast", "")),
            "v3_inbox_count": None,
            "v3_supervisor": None,
            "v3_supervisor_state": "unknown",
        }
        try:
            with self.database.session() as session:
                values["v3_inbox_count"] = session.scalar(
                    select(func.count()).select_from(MangaRecord).where(
                        MangaRecord.status.in_(("manual_review", "quarantined"))
                    )
                )
                control = session.get(SystemControl, "supervisor")
                values["v3_supervisor"] = control
                values["v3_supervisor_state"] = _supervisor_status(
                    control, self.supervisor_config.poll_seconds
                )
        except SQLAlchemyError:
            values["v3_database_error"] = True
        return values

    def page(self, request, name: str, *, status_code: int = 200, headers=None, **values):
        from ..app import _context

        context = _context(request, **values)
        context.update(self.shell(request))
        return self.templates.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code, headers=headers
        )

    def partial(self, request, name: str, *, status_code: int = 200, headers=None, **values):
        from ..app import _context

        return self.templates.TemplateResponse(
            request=request, name=name, context=_context(request, **values),
            status_code=status_code, headers=headers,
        )

    def error(self, request, message: str, status_code: int = 400):
        """HTMX gets a fragment for the form's error slot; plain requests get a page."""
        if is_htmx(request) and request.headers.get("HX-Boosted") != "true":
            return self.partial(
                request, "uiv3/_error_fragment.html", status_code=status_code,
                message=message, status_code_value=status_code,
            )
        return self.page(
            request, "uiv3/error.html", status_code=status_code,
            message=message, status_code_value=status_code,
        )

    def service_error(self, request, exc: Exception):
        from ...special.core.service import SpecialServiceError
        from ..configuration import ConfigurationError
        from ..services import WebServiceError

        if isinstance(exc, WebServiceError):
            return self.error(request, str(exc), exc.status_code)
        if isinstance(exc, SpecialServiceError):
            from ...special.service import SpecialConflict, SpecialNotFound

            status = 404 if isinstance(exc, SpecialNotFound) else 409 if isinstance(
                exc, SpecialConflict
            ) else 400
            return self.error(request, str(exc), status)
        if isinstance(exc, ConfigurationError):
            return self.error(request, str(exc), 422)
        return self.error(request, str(exc) or "表单数据无效", 400)


def html(text: str, status_code: int = 200, headers=None) -> HTMLResponse:
    return HTMLResponse(text, status_code=status_code, headers=headers)
