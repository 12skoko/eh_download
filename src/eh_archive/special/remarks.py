from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..db.models import MangaRecord, SpecialWorkflow

START_MARKER = "[special-processing]"
END_MARKER = "[/special-processing]"
_BLOCK = re.compile(
    re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
    flags=re.DOTALL | re.IGNORECASE,
)

PHASE_LABELS = {
    "awaiting_torrent_load": "等待加载 Torrent 列表",
    "loading_torrent_options": "正在加载 Torrent 列表",
    "awaiting_torrent_selection": "等待用户选择图片和视频 Torrent",
    "torrent_submit_queued": "等待提交 Torrent",
    "submitting_torrents": "正在提交 Torrent 到 qBittorrent",
    "downloading": "下载中（等待人工检查）",
    "checking_downloads": "正在检查下载并准备整合",
    "extracting": "正在安全解压",
    "converting": "正在转换 MP4 为 WebP",
    "packing": "正在打包最终档案",
    "ready": "整合完成，已回到普通校验流程",
    "failed": "处理失败，等待人工操作",
    "cancelling": "正在取消和清理",
    "cancelled": "已取消",
}

SOURCE_CLEANUP_LABELS = {
    "pending": "等待档案完成后手动清理",
    "queued": "已手动排队",
    "running": "正在清理",
    "completed": "已清理",
    "failed": "清理失败，等待手动重试",
    "retained_on_forced_exit": "已退出并保留资源",
}


def user_remark(value: str | None) -> str:
    return _BLOCK.sub("", value or "").strip()


def replace_user_remark(existing: str | None, manual: str | None) -> str | None:
    """Replace human text while preserving the current system-owned summary."""

    system_match = _BLOCK.search(existing or "")
    clean_manual = (manual or "").strip()
    if system_match is None:
        return clean_manual or None
    system = system_match.group(0)
    return f"{clean_manual}\n\n{system}" if clean_manual else system


def _percent(value: Any) -> str:
    try:
        return f"{max(0.0, min(1.0, float(value))) * 100:.0f}%"
    except (TypeError, ValueError):
        return "未知"


def workflow_summary(workflow: SpecialWorkflow, *, timezone: str = "UTC") -> str:
    progress = dict(workflow.progress or {})
    payload = dict(workflow.payload or {})
    torrents = {
        str(item.get("role")): item
        for item in payload.get("torrents", [])
        if isinstance(item, dict)
    }
    if torrents:
        progress_text = (
            f"图片 {_percent(torrents.get('image', {}).get('progress'))}，"
            f"视频 {_percent(torrents.get('video', {}).get('progress'))}"
        )
    elif progress.get("total"):
        progress_text = f"{progress.get('completed', 0)}/{progress['total']}"
    else:
        progress_text = str(progress.get("message") or "—")
    updated = workflow.updated_at
    if isinstance(updated, datetime):
        try:
            updated_text = updated.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError):
            updated_text = updated.isoformat(timespec="seconds")
    else:
        updated_text = "—"
    result = {
        "completed": "成功",
        "cancelled": "已取消",
        "failed": "失败",
    }.get(workflow.status)
    lines = [
        START_MARKER,
        "模块：视频种子下载与整合"
        if workflow.kind == "video_archive"
        else f"模块：{workflow.kind}",
        f"阶段：{PHASE_LABELS.get(workflow.phase, workflow.phase)}",
        f"进度：{progress_text}",
        f"最后更新：{updated_text}",
    ]
    raw_cleanup = payload.get("source_cleanup")
    cleanup = dict(raw_cleanup) if isinstance(raw_cleanup, dict) else {}
    cleanup_status = str(cleanup.get("status", ""))
    if cleanup_status:
        lines.append(
            "源文件清理：" + SOURCE_CLEANUP_LABELS.get(cleanup_status, cleanup_status)
        )
    if result:
        lines.append(f"结果：{result}")
    lines.append(END_MARKER)
    return "\n".join(lines)


def sync_remark(
    manga: MangaRecord,
    workflow: SpecialWorkflow,
    *,
    timezone: str = "UTC",
) -> None:
    manual = user_remark(manga.remark)
    summary = workflow_summary(workflow, timezone=timezone)
    manga.remark = f"{manual}\n\n{summary}" if manual else summary


def restore_entry_error(
    manga: MangaRecord,
    workflow: SpecialWorkflow,
    *,
    restored_at: datetime,
) -> None:
    """Restore the manual-review reason captured when the workflow started."""

    entry = dict((workflow.payload or {}).get("entry") or {})
    code = entry.get("source_error_code")
    manga.last_error_operation = entry.get("source_error_operation") if code else None
    manga.last_error_code = str(code) if code else None
    manga.last_error_detail = entry.get("source_error_detail") if code else None
    manga.last_error_at = restored_at if code else None
