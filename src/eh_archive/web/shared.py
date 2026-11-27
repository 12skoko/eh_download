from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Request


from ..config.loader import SUPERVISOR_MODULES
from ..db.models import SystemControl, SystemHealth
from ..services.paths import safe_filename
from ..special.remarks import PHASE_LABELS
from .auth import WebIdentity
from .services import (
    BULK_STATUS_TARGETS,
    COMPONENT_LABELS,
    CONTROL_COMPONENTS,
    DOWNLOAD_METHOD_LOCATIONS,
    MANUAL_STATUS_TARGETS,
    STATUS_LABELS,
    WebService,
    WebServiceError,
    allowed_actions,
    serialize_manga,
)


def _filter_query(params: list[tuple[str, str]]) -> str:
    """Build a &-joined query string from filter params, for pagination links."""
    return urlencode(params)


def _context(request, **values):
    identity = getattr(request.state, "identity", WebIdentity("local", "local", 0))
    return {
        "identity": identity,
        "csrf_token": identity.csrf_token,
        "status_labels": STATUS_LABELS,
        "component_labels": COMPONENT_LABELS,
        "control_components": CONTROL_COMPONENTS,
        "supervisor_modules": SUPERVISOR_MODULES,
        "allowed_actions": allowed_actions,
        "manual_status_targets": MANUAL_STATUS_TARGETS,
        "bulk_status_targets": BULK_STATUS_TARGETS,
        "now": datetime.now(UTC),
        "special_phase_labels": PHASE_LABELS,
        **values,
    }


async def _login_form(request: Request):
    return await request.form()


async def _validated_form(request: Request):
    """Read/validate on the event loop; sync endpoints run wholly in the thread pool."""
    form = await request.form()
    identity = getattr(request.state, "identity", None)
    if identity is None or str(form.get("csrf_token", "")) != identity.csrf_token:
        from fastapi import HTTPException

        raise HTTPException(403, "CSRF validation failed")
    return form


def _actor(request) -> str:
    return f"web:{request.state.identity.username}"


def _api_update(database, request, callback, *, app_config=None):
    from fastapi import HTTPException

    try:
        with database.session() as session:
            return serialize_manga(
                callback(WebService(session, actor=_actor(request), app_config=app_config))
            )
    except WebServiceError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


def _optional_text(value) -> str | None:
    text_value = str(value).strip() if value is not None else ""
    return text_value or None


def _optional_int(value) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(str(value))


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


def _is_loopback(host: str) -> bool:
    return host.casefold() in {"127.0.0.1", "localhost", "::1"}


def _health_status(row: SystemHealth, interval_seconds: float) -> str:
    age = _age_seconds(row.checked_at)
    return "stale" if age > max(interval_seconds * 3, 180) else row.status


def _supervisor_status(row: SystemControl | None, poll_seconds: float) -> str:
    if row is None or row.heartbeat_at is None:
        return "unknown"
    if row.lease_owner is None:
        return "stale"
    if row.lease_until is not None and _age_seconds(row.lease_until) >= 0:
        return "stale"
    age = _age_seconds(row.heartbeat_at)
    return "stale" if age > max(poll_seconds * 6, 30) else row.state


def _age_seconds(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (datetime.now(UTC) - value).total_seconds()


def _format_collected_at(value, timezone) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None:
            return str(value)
        return parsed.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, AttributeError):
        return str(value)


def _format_full_collect_datetime(value, timezone: str) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, AttributeError):
        return str(value)


def _format_datetime(value, timezone: str = "UTC") -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _format_filesize(value) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


def _format_duration(value) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(float(value)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _attempt_progress(attempt) -> dict[str, float | int | None]:
    downloaded = max(0, int(attempt.progress_bytes or 0))
    total = (
        max(0, int(attempt.progress_total_bytes))
        if attempt.progress_total_bytes is not None
        else None
    )
    speed = max(0.0, float(attempt.progress_speed_bps or 0))
    percent = min(100.0, downloaded / total * 100) if total else None
    eta = max(0.0, (total - downloaded) / speed) if total and speed > 0 else None
    return {
        "downloaded": downloaded,
        "total": total,
        "speed": speed,
        "percent": percent,
        "eta": eta,
    }


def _manual_artifact_directories(app_config, manga_id: str) -> dict[str, str]:
    directories: dict[str, str] = {}
    for method, location in DOWNLOAD_METHOD_LOCATIONS.items():
        try:
            directory = app_config.root(location).expanduser().resolve()
        except KeyError:
            continue
        if method == "torrent":
            directory = directory / safe_filename(manga_id.split("/", 1)[0])
        directories[method] = str(directory)
    return directories


def _manga_tab_id(value) -> str:
    manga_id = str(value or "").strip()
    return manga_id.partition("/")[0] or manga_id


def _error_summary(value, error_code=None) -> str:
    detail = str(value or "").strip()
    if not detail:
        return "未记录原因"
    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        for key in ("error", "message", "detail"):
            summary = parsed.get(key)
            if isinstance(summary, str) and summary.strip():
                detail = summary.strip()
                break
    if re.search(r"<!doctype\s+html\b|<html\b|<head\b|<body\b", detail, re.IGNORECASE):
        if error_code == "lrr_metadata_non_json_response":
            return "LANraragi 元数据接口返回了 HTML 页面，预期为 JSON；响应片段请展开查看。"
        return "服务返回了 HTML 页面；响应片段请展开查看。"
    summary = " ".join(detail.split())
    return summary if len(summary) <= 200 else summary[:199] + "…"
