"""Shared helpers for the /uiv2 interface.

The v2 layer only changes presentation and navigation. Every write goes through
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

PREFIX = "/uiv2"
STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "uiv2"

# 流水线分组：(键, 标题, 状态, 所属模块)。24 个状态全部出现且只出现一次。
PIPELINE = (
    ("collect", "采集与筛选", ("deferred", "discovered"), ("collect", "screen")),
    ("download", "下载", ("download_pending", "downloading", "download_blocked"),
     ("details", "torrent_download", "torrent_check", "direct_download")),
    ("verify", "校验与打包", ("downloaded", "rename_pending", "validating", "preparing"),
     ("validate", "prepare")),
    ("upload", "上传", ("upload_pending", "uploading", "uploaded"), ("upload", "cleanup")),
    ("done", "完成", ("completed",), ()),
)
SIDE_GROUPS = (
    ("exceptions", "异常与旁路", "triangle-alert",
     ("manual_review", "quarantined", "special_processing", "unavailable",
      "cancel_requested", "cancelled"), ("special_processing",)),
    ("deletion", "删除队列", "trash-2", ("outdated", "force_delete_pending", "deleted"), ("delete",)),
    ("excluded", "未收录", "ban", ("filtered_out", "skipped"), ()),
)
STATUS_GROUPS = tuple((title, statuses) for _, title, statuses, _ in PIPELINE) + (
    ("异常与旁路", SIDE_GROUPS[0][3]),
    ("删除与未收录", SIDE_GROUPS[1][3] + SIDE_GROUPS[2][3]),
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

# 档案状态的唯一配色映射；未列出的状态用正文色。运行状态和操作风险独立配色。
_STATUS_TONES = {
    "completed": "ok", "deferred": "warn", "manual_review": "danger",
    "unavailable": "muted", "deleted": "muted", "filtered_out": "muted", "skipped": "muted",
}
_RUN_TONES = {
    "queued": "idle", "running": "run", "succeeded": "ok", "completed": "ok", "failed": "danger",
    "cancelled": "muted", "active": "special", "pending": "idle", "interrupted": "danger",
    "healthy": "ok", "degraded": "warn", "stale": "muted", "unknown": "muted", "error": "danger",
    "unhealthy": "danger", "paused": "muted", "draining": "warn",
}
RUN_LABELS = {
    "queued": "排队中", "running": "运行中", "succeeded": "成功", "failed": "失败",
    "cancelled": "已取消", "active": "进行中", "completed": "已完成", "pending": "待执行",
    "healthy": "正常", "degraded": "降级", "stale": "过期", "unknown": "未知", "error": "异常",
    "unhealthy": "异常", "paused": "已暂停", "draining": "排空中",
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
    return _STATUS_TONES.get(status or "", "body")


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
    for name in ("uiv2.css", "uiv2.js", "uiv2-pages.js", "uiv2-ops.js"):
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
            {"uiv2:toast": {"message": toast, "tone": tone_name}}, ensure_ascii=True
        )
    after = {"uiv2:" + name.replace("_", "-"): value for name, value in events.items()}
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
class Uiv2:
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
            "v2_old_url": old_url(request),
            "v2_toast": TOASTS.get(request.query_params.get("toast", "")),
            "v2_inbox_count": None,
            "v2_supervisor": None,
            "v2_supervisor_state": "unknown",
        }
        try:
            with self.database.session() as session:
                values["v2_inbox_count"] = session.scalar(
                    select(func.count()).select_from(MangaRecord).where(
                        MangaRecord.status.in_(("manual_review", "quarantined"))
                    )
                )
                control = session.get(SystemControl, "supervisor")
                values["v2_supervisor"] = control
                values["v2_supervisor_state"] = _supervisor_status(
                    control, self.supervisor_config.poll_seconds
                )
        except SQLAlchemyError:
            values["v2_database_error"] = True
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
                request, "uiv2/_error_fragment.html", status_code=status_code,
                message=message, status_code_value=status_code,
            )
        return self.page(
            request, "uiv2/error.html", status_code=status_code,
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
