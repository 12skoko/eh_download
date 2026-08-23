from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config.loader import SUPERVISOR_MODULES, AppConfig
from ..db.models import (
    DOWNLOAD_METHOD_VALUES,
    EventLog,
    JobAttempt,
    MangaRecord,
    SystemControl,
    SystemHealth,
)
from ..db.repository import utcnow
from ..domain.states import Status, can_transition, transition_target
from ..services.paths import ArtifactPathService, UnsafePathError, safe_filename

CONTROL_COMPONENTS = ("supervisor", *SUPERVISOR_MODULES)
DOWNLOAD_METHOD_LOCATIONS = {
    "torrent": "torrent_download",
    "direct": "direct_download",
    "hah": "hah_download",
    "aria2": "aria2_download",
}

STATUS_LABELS = {
    Status.DISCOVERED.value: "已发现",
    Status.DEFERRED.value: "观察等待",
    Status.DOWNLOAD_PENDING.value: "等待下载",
    Status.DOWNLOADING.value: "下载中",
    Status.DOWNLOADED.value: "已下载",
    Status.VALIDATING.value: "校验中",
    Status.PREPARING.value: "准备中",
    Status.UPLOAD_PENDING.value: "等待上传",
    Status.UPLOADING.value: "上传中",
    Status.UPLOADED.value: "已上传",
    Status.COMPLETED.value: "已完成",
    Status.QUARANTINED.value: "已隔离",
    Status.MANUAL_REVIEW.value: "人工复核",
    Status.SKIPPED.value: "已跳过",
    Status.UNAVAILABLE.value: "不可用",
    Status.OUTDATED.value: "已过时",
    Status.FORCE_DELETE_PENDING.value: "等待强制删除",
    Status.RENAME_PENDING.value: "等待冲突改名",
    Status.DELETED.value: "已删除",
    Status.CANCEL_REQUESTED.value: "等待取消",
    Status.CANCELLED.value: "已取消",
}

COMPONENT_LABELS = {
    "supervisor": "Supervisor",
    "collect": "采集",
    "details": "详情补全",
    "torrent_download": "Torrent 下载",
    "direct_download": "直接下载",
    "validate": "校验",
    "prepare": "压缩准备",
    "upload": "上传",
    "cleanup": "清理",
    "delete": "档案删除",
    "qbittorrent": "qBittorrent",
    "lanraragi": "LANraragi",
}

ACTION_LABELS = {
    "retry": "重试",
    "skip": "跳过",
    "cancel": "取消",
    "resume": "恢复下载",
    "validate": "重新校验",
    "upload": "恢复上传",
    "confirm-uploaded": "确认已上传",
}

MANUAL_STATUS_TARGETS = (
    {
        "status": Status.DISCOVERED.value,
        "label": "已发现",
        "description": "停止当前流程并退回已发现状态；不会自动进入下载队列。",
    },
    {
        "status": Status.DOWNLOAD_PENDING.value,
        "label": "等待下载",
        "description": "保存指定下载方式，等待 Supervisor 安排下载。",
    },
    {
        "status": Status.DOWNLOADED.value,
        "label": "已下载",
        "description": "登记已有档案，等待 Supervisor 安排校验。",
    },
    {
        "status": Status.COMPLETED.value,
        "label": "已完成",
        "description": "人工确认整个流程完成；不会执行 cleanup 或删除本地文件。",
    },
    {
        "status": Status.MANUAL_REVIEW.value,
        "label": "人工复核",
        "description": "停止自动领取，保留记录供人工检查。",
    },
    {
        "status": Status.SKIPPED.value,
        "label": "已跳过",
        "description": "从自动流程中跳过这条档案。",
    },
    {
        "status": Status.UNAVAILABLE.value,
        "label": "不可用",
        "description": "标记来源或档案不可用，必须填写原因。",
        "requires_reason": True,
    },
    {
        "status": Status.QUARANTINED.value,
        "label": "已隔离",
        "description": "标记档案需要隔离处理，必须填写原因。",
        "requires_reason": True,
    },
    {
        "status": Status.OUTDATED.value,
        "label": "已过时",
        "description": "关联替代档案并进入删除队列；delete 模块可能随后执行实际删除。",
        "danger": True,
    },
    {
        "status": Status.FORCE_DELETE_PENDING.value,
        "label": "强制删除",
        "description": (
            "进入 delete 队列并跳过替代档案验证；随后会删除 LANraragi 档案和本地归档文件。"
        ),
        "requires_reason": True,
        "danger": True,
        "web_only": True,
        "allowed_from": (
            Status.UPLOADED.value,
            Status.COMPLETED.value,
            Status.OUTDATED.value,
            Status.MANUAL_REVIEW.value,
        ),
    },
    {
        "status": Status.DELETED.value,
        "label": "已删除",
        "description": "只确认外部删除事实，不会在此操作中删除任何文件。",
        "requires_reason": True,
        "danger": True,
    },
)

MANUAL_STATUS_VALUES = frozenset(target["status"] for target in MANUAL_STATUS_TARGETS)
FORCE_DELETE_SOURCE_STATUSES = frozenset(
    {
        Status.UPLOADED.value,
        Status.COMPLETED.value,
        Status.OUTDATED.value,
        Status.MANUAL_REVIEW.value,
    }
)
_ACTION_EVENTS: dict[str, dict[str, str]] = {
    "retry": {
        Status.UNAVAILABLE.value: "retry",
        Status.SKIPPED.value: "override",
        Status.QUARANTINED.value: "redownload",
        Status.CANCELLED.value: "resume",
        Status.MANUAL_REVIEW.value: "resume_download",
    },
    "skip": {Status.DISCOVERED.value: "skip"},
    "cancel": {
        status.value: "cancel"
        for status in (
            Status.DISCOVERED,
            Status.DEFERRED,
            Status.DOWNLOAD_PENDING,
            Status.DOWNLOADING,
            Status.DOWNLOADED,
            Status.UPLOAD_PENDING,
            Status.SKIPPED,
            Status.UNAVAILABLE,
            Status.QUARANTINED,
            Status.MANUAL_REVIEW,
        )
    },
    "resume": {
        Status.MANUAL_REVIEW.value: "resume_download",
        Status.CANCELLED.value: "resume",
    },
    "validate": {
        Status.DOWNLOADED.value: "validate",
        Status.MANUAL_REVIEW.value: "resume_validate",
        Status.CANCELLED.value: "resume_validate",
    },
    "upload": {
        Status.VALIDATING.value: "upload",
        Status.MANUAL_REVIEW.value: "resume_upload",
        Status.CANCELLED.value: "resume_upload",
    },
    "confirm-uploaded": {Status.MANUAL_REVIEW.value: "confirm_uploaded"},
}


class WebServiceError(Exception):
    status_code = 400


class NotFound(WebServiceError):
    status_code = 404


class Conflict(WebServiceError):
    status_code = 409


class InvalidRequest(WebServiceError):
    status_code = 400


@dataclass(frozen=True)
class MangaPage:
    rows: list[MangaRecord]
    next_cursor: str | None


@dataclass(frozen=True)
class ReviewFacet:
    error_code: str
    count: int


class WebService:
    def __init__(
        self,
        session: Session,
        *,
        actor: str,
        app_config: AppConfig | None = None,
    ) -> None:
        self.session = session
        self.actor = actor
        self.app_config = app_config

    def add_manga(
        self,
        *,
        url: str,
        manga_id: str,
        priority: int,
        remark: str | None,
        row_version: int | None = None,
    ) -> MangaRecord:
        row = self.session.get(MangaRecord, manga_id)
        if row is None:
            row = MangaRecord(
                manga_id=manga_id,
                name=manga_id,
                link=url,
                queue_source="manual",
                priority=priority,
                status=Status.DOWNLOAD_PENDING.value,
                remark=remark,
            )
            self.session.add(row)
            self.session.flush()
            self._event(
                row,
                "add",
                to_status=row.status,
                detail={"url": url, "priority": priority, "reason": remark},
            )
            return row
        self._require_version(row, row_version)
        previous = row.status
        row.priority = priority
        row.remark = remark
        if row.status in {
            Status.SKIPPED.value,
            Status.UNAVAILABLE.value,
            Status.MANUAL_REVIEW.value,
        }:
            row.status = Status.DOWNLOAD_PENDING.value
            row.status_updated_at = utcnow()
            row.next_retry_at = None
        row.updated_at = utcnow()
        row.row_version += 1
        self._event(
            row,
            "add",
            from_status=previous,
            to_status=row.status,
            detail={"priority": priority, "reason": remark},
        )
        return row

    def update_remark(self, manga_id: str, *, remark: str | None, row_version: int) -> MangaRecord:
        row = self._manga(manga_id)
        self._require_version(row, row_version)
        row.remark = remark
        row.updated_at = utcnow()
        row.row_version += 1
        self._event(row, "remark", detail={"changed": True})
        return row

    def update_priority(self, manga_id: str, *, priority: int, row_version: int) -> MangaRecord:
        row = self._manga(manga_id)
        self._require_version(row, row_version)
        previous = row.priority
        row.priority = priority
        row.updated_at = utcnow()
        row.row_version += 1
        self._event(row, "priority", detail={"from": previous, "to": priority})
        return row

    def action(
        self,
        manga_id: str,
        action: str,
        *,
        row_version: int,
        reason: str | None = None,
        archive_id: str | None = None,
    ) -> MangaRecord:
        row = self._manga(manga_id)
        self._require_version(row, row_version)
        event = _ACTION_EVENTS.get(action, {}).get(row.status)
        if event is None or not can_transition(row.status, event):
            raise InvalidRequest("当前状态不支持这个操作")
        confirmed_id = None
        if action == "confirm-uploaded":
            if not archive_id or not re.fullmatch(r"[0-9a-fA-F]{40}", archive_id):
                raise InvalidRequest("archive ID 必须是 40 位 SHA1")
            confirmed_id = archive_id.lower()
        previous = row.status
        row.status = transition_target(row.status, event).value
        if confirmed_id:
            row.lrr_archive_id = confirmed_id
        if action in {"retry", "resume", "validate", "upload"}:
            row.next_retry_at = None
        row.status_updated_at = row.updated_at = utcnow()
        row.queue_source = "manual"
        row.row_version += 1
        self._event(
            row,
            action,
            from_status=previous,
            to_status=row.status,
            detail={
                **({"reason": reason.strip()} if reason and reason.strip() else {}),
                **({"archive_id": confirmed_id} if confirmed_id else {}),
            },
        )
        return row

    def override_status(
        self,
        manga_id: str,
        *,
        target_status: str,
        row_version: int,
        reason: str | None = None,
        download_method: str | None = None,
        artifact_filename: str | None = None,
        archive_id: str | None = None,
        superseded_by_id: str | None = None,
        confirmation_manga_id: str | None = None,
        allow_web_only: bool = False,
    ) -> MangaRecord:
        """Apply an explicit, audited administrator status override."""

        row = self._manga(manga_id)
        self._require_version(row, row_version)
        if target_status not in MANUAL_STATUS_VALUES:
            raise InvalidRequest("这个状态不能通过人工控制设置")
        if target_status == Status.FORCE_DELETE_PENDING.value:
            if not allow_web_only:
                raise InvalidRequest("等待强制删除状态只能通过管理网页设置")
            if row.status not in FORCE_DELETE_SOURCE_STATUSES:
                raise InvalidRequest("当前状态不能进入强制删除队列")
            if not confirmation_manga_id or confirmation_manga_id.strip() != row.manga_id:
                raise InvalidRequest("二次确认的档案 ID 与当前档案不一致")
        if row.status == target_status:
            raise InvalidRequest("档案已经处于目标状态")
        if row.active_attempt_id is not None or row.lease_owner or row.lease_token:
            raise Conflict("档案仍有活动任务或租约，不能人工修改状态")

        clean_reason = reason.strip() if reason and reason.strip() else None
        if (
            target_status
            in {
                Status.UNAVAILABLE.value,
                Status.QUARANTINED.value,
                Status.FORCE_DELETE_PENDING.value,
                Status.DELETED.value,
            }
            and not clean_reason
        ):
            raise InvalidRequest("这个状态必须填写操作原因")

        clean_method = download_method.strip() if download_method else None
        previous_method = row.download_method
        previous_artifact_location = row.artifact_location
        if target_status in {Status.DOWNLOAD_PENDING.value, Status.DOWNLOADED.value}:
            if clean_method not in DOWNLOAD_METHOD_VALUES:
                raise InvalidRequest("必须选择有效的下载方式")
            row.download_method = clean_method
            if clean_method != previous_method:
                row.external_download_id = None

        clean_filename = artifact_filename.strip() if artifact_filename else None
        artifact_path = None
        if target_status == Status.DOWNLOADED.value:
            if not clean_filename:
                raise InvalidRequest("进入已下载状态必须填写文件名")
            row.artifact_location = DOWNLOAD_METHOD_LOCATIONS[clean_method]
            artifact_path = self._require_artifact(row, clean_filename)
            try:
                artifact_is_directory = artifact_path.is_dir()
                artifact_size = artifact_path.stat().st_size if artifact_path.is_file() else None
            except OSError as exc:
                raise InvalidRequest("无法读取本地档案信息") from exc
            row.artifact_filename = clean_filename
            row.artifact_kind = (
                "directory"
                if artifact_is_directory
                else "zip"
                if artifact_path.suffix.casefold() == ".zip"
                else "file"
            )
            row.artifact_size = artifact_size
            row.artifact_sha1 = None
            row.artifact_checked_at = None
        elif target_status == Status.UPLOAD_PENDING.value:
            artifact_path = self._require_artifact(row, row.artifact_filename)
            if not artifact_path.is_file() or not row.artifact_sha1 or row.artifact_size is None:
                raise InvalidRequest("本地档案尚未完成校验，请先进入已下载状态重新校验")

        confirmed_id = archive_id.strip().lower() if archive_id else None
        if target_status in {Status.UPLOADED.value, Status.COMPLETED.value}:
            if not confirmed_id or not re.fullmatch(r"[0-9a-f]{40}", confirmed_id):
                raise InvalidRequest("archive ID 必须是 40 位十六进制字符串")
            row.lrr_archive_id = confirmed_id

        replacement_id = superseded_by_id.strip() if superseded_by_id else None
        if target_status == Status.OUTDATED.value:
            if not replacement_id:
                raise InvalidRequest("标记过时必须填写替代档案 ID")
            if replacement_id == row.manga_id:
                raise InvalidRequest("替代档案不能是当前档案")
            replacement = self.session.get(MangaRecord, replacement_id)
            if replacement is None:
                raise InvalidRequest("替代档案不存在")
            row.superseded_by_id = replacement_id

        previous_status = row.status
        row.rename_target_filename = None
        if target_status == Status.DISCOVERED.value:
            row.screen_pending = False
            row.screen_group_id = None
        row.status = target_status
        row.defer_until = None
        row.next_retry_at = None
        row.queue_source = "manual"
        row.status_updated_at = row.updated_at = utcnow()
        row.row_version += 1
        detail: dict[str, Any] = {
            "reason": clean_reason,
            "download_method": row.download_method,
            "artifact_location": row.artifact_location,
            "artifact_filename": row.artifact_filename,
            "artifact_check_path": str(artifact_path) if artifact_path is not None else None,
            "archive_id": confirmed_id,
            "superseded_by_id": replacement_id,
            "confirmation_manga_id": (
                row.manga_id if target_status == Status.FORCE_DELETE_PENDING.value else None
            ),
        }
        if previous_method != row.download_method:
            detail["previous_download_method"] = previous_method
        if previous_artifact_location != row.artifact_location:
            detail["previous_artifact_location"] = previous_artifact_location
        self._event(
            row,
            "status_override",
            from_status=previous_status,
            to_status=target_status,
            detail={key: value for key, value in detail.items() if value is not None},
        )
        return row

    def request_conflict_rename(
        self,
        manga_id: str,
        *,
        row_version: int,
        target_filename: str | None,
        reason: str | None,
        confirmed: bool,
    ) -> MangaRecord:
        """Queue a Web-approved filename-conflict recovery for validation."""

        row = self._manga(manga_id)
        self._require_version(row, row_version)
        if row.status != Status.MANUAL_REVIEW.value:
            raise InvalidRequest("只有人工复核状态可以申请冲突改名")
        duplicate_error = bool(
            row.last_error_code and row.last_error_code.casefold() == "lrr_409"
        )
        if not duplicate_error and not row.rename_target_filename:
            raise InvalidRequest("只有 LANraragi 409 同名冲突可以使用这个操作")
        if row.active_attempt_id is not None or row.lease_owner or row.lease_token:
            raise Conflict("档案仍有活动任务或租约，不能申请冲突改名")
        if not confirmed:
            raise InvalidRequest("必须确认同名档案需要分别保留")
        clean_reason = reason.strip() if reason and reason.strip() else None
        if not clean_reason:
            raise InvalidRequest("必须填写分别保留的原因")
        if not row.artifact_filename:
            raise InvalidRequest("档案缺少当前文件名")
        source = self._require_artifact(row, row.artifact_filename)
        if not source.is_file():
            raise InvalidRequest("冲突改名只支持已经生成的归档文件")

        requested = target_filename.strip() if target_filename else ""
        if not requested:
            raise InvalidRequest("必须填写改名后的文件名")
        try:
            clean_target = safe_filename(requested)
        except (OSError, UnsafePathError, ValueError) as exc:
            raise InvalidRequest("改名后的文件名无效") from exc
        if clean_target != requested:
            raise InvalidRequest("改名后的文件名包含不安全字符或长度超限")
        if clean_target == row.artifact_filename:
            raise InvalidRequest("改名后的文件名不能与当前文件名相同")
        if Path(clean_target).suffix.casefold() != Path(row.artifact_filename).suffix.casefold():
            raise InvalidRequest("冲突改名不能改变文件扩展名")
        target = self._artifact_path(row, clean_target)
        if target.exists():
            raise Conflict("改名后的目标文件已经存在，请更换文件名")

        previous_status = row.status
        previous_error_code = row.last_error_code
        row.rename_target_filename = clean_target
        row.artifact_size = None
        row.artifact_sha1 = None
        row.artifact_checked_at = None
        row.status = transition_target(row.status, "rename").value
        row.queue_source = "manual"
        row.next_retry_at = None
        row.status_updated_at = row.updated_at = utcnow()
        row.row_version += 1
        self._event(
            row,
            "conflict_rename",
            from_status=previous_status,
            to_status=row.status,
            detail={
                "reason": clean_reason,
                "source_filename": row.artifact_filename,
                "target_filename": clean_target,
                "source_error_code": previous_error_code,
            },
        )
        return row

    def release_expired_lease(
        self,
        manga_id: str,
        *,
        row_version: int,
        reason: str | None,
        confirmed: bool,
    ) -> MangaRecord:
        """Abandon an expired attempt and move its archive to manual review."""

        row = self._manga(manga_id)
        self._require_version(row, row_version)
        clean_reason = reason.strip() if reason and reason.strip() else None
        if not clean_reason:
            raise InvalidRequest("解除过期租约必须填写原因")
        if not confirmed:
            raise InvalidRequest("请确认旧任务进程已经停止或不应再写回数据库")
        if row.active_attempt_id is None:
            raise InvalidRequest("档案没有活动 attempt")
        now = utcnow()
        if row.lease_until is None or not _lease_expired(row.lease_until, now=now):
            raise Conflict("档案租约尚未过期，不能解除")

        attempt = self.session.get(JobAttempt, row.active_attempt_id)
        if attempt is None or attempt.manga_id != row.manga_id:
            raise Conflict("活动 attempt 记录不存在或不属于当前档案")
        if attempt.status != "running":
            raise Conflict("活动 attempt 已经结束，请刷新页面")
        if not row.lease_token or attempt.lease_token != row.lease_token:
            raise Conflict("档案与 attempt 的租约 token 不一致，不能自动解除")

        previous_status = row.status
        previous_lease_owner = row.lease_owner
        previous_lease_until = row.lease_until
        attempt.status = "abandoned"
        attempt.finished_at = now
        attempt.resulting_status = Status.MANUAL_REVIEW.value
        attempt.error_code = "lease_expired_manual_release"
        attempt.detail = {
            **(attempt.detail or {}),
            "manual_release": {
                "actor": self.actor,
                "reason": clean_reason,
                "released_at": now.isoformat(),
                "lease_owner": previous_lease_owner,
                "lease_until": previous_lease_until.isoformat(),
            },
        }

        row.status = Status.MANUAL_REVIEW.value
        row.queue_source = "manual"
        row.defer_until = None
        row.next_retry_at = None
        row.active_attempt_id = None
        row.lease_token = None
        row.lease_owner = None
        row.lease_until = None
        row.status_updated_at = row.updated_at = now
        row.row_version += 1
        self._event(
            row,
            "release_expired_lease",
            attempt_id=attempt.id,
            from_status=previous_status,
            to_status=Status.MANUAL_REVIEW.value,
            error_code="lease_expired_manual_release",
            detail={
                "reason": clean_reason,
                "attempt_id": attempt.id,
                "attempt_operation": attempt.operation,
                "attempt_started_at": attempt.started_at.isoformat(),
                "lease_owner": previous_lease_owner,
                "lease_until": previous_lease_until.isoformat(),
                "external_task_id": attempt.external_task_id,
                "external_effect_started_at": (
                    attempt.external_effect_started_at.isoformat()
                    if attempt.external_effect_started_at
                    else None
                ),
            },
        )
        return row

    def _artifact_path(self, row: MangaRecord, filename: str | None) -> Path:
        if self.app_config is None:
            raise InvalidRequest("Web 未加载存储路径配置")
        if not row.artifact_location:
            raise InvalidRequest("档案缺少 artifact_location，不能验证文件")
        if not filename:
            raise InvalidRequest("档案缺少文件名")
        paths = ArtifactPathService(self.app_config)
        try:
            path = (
                paths.torrent_registered(row.manga_id, filename)
                if row.artifact_location == "torrent_download"
                else paths.validate_registered(row.artifact_location, filename)
            )
        except (KeyError, OSError, UnsafePathError, ValueError) as exc:
            raise InvalidRequest("档案文件名或存储位置无效") from exc
        return path

    def _require_artifact(self, row: MangaRecord, filename: str | None) -> Path:
        path = self._artifact_path(row, filename)
        if not path.exists() or not (path.is_file() or path.is_dir()):
            raise InvalidRequest(f"找不到本地档案：{path}")
        return path

    def set_control(
        self,
        component: str,
        *,
        state: str,
        reason: str | None,
        row_version: int | None,
    ) -> SystemControl:
        if component == "all":
            raise InvalidRequest("component all was replaced by supervisor")
        if component not in CONTROL_COMPONENTS:
            raise InvalidRequest("未知组件")
        allowed_states = (
            {"running", "paused", "draining"}
            if component == "supervisor"
            else {"running", "paused"}
        )
        if state not in allowed_states:
            raise InvalidRequest("组件不支持这个控制状态")
        row = self.session.get(SystemControl, component)
        if row is None:
            row = SystemControl(
                component=component,
                state=state,
                reason=reason,
                updated_by=self.actor,
            )
            self.session.add(row)
        else:
            if row_version is None:
                raise Conflict("缺少 row_version，请刷新后重试")
            if row.row_version != row_version:
                raise Conflict("记录已被其他进程修改，请刷新后重试")
            row.state = state
            row.reason = reason
            row.updated_by = self.actor
            row.row_version += 1
        self.session.add(
            EventLog(
                component="web",
                event_type="manual",
                operation="control",
                actor=self.actor,
                detail={"component": component, "state": state, "reason": reason},
            )
        )
        return row

    def _manga(self, manga_id: str) -> MangaRecord:
        row = self.session.get(MangaRecord, manga_id)
        if row is None:
            raise NotFound("档案不存在")
        return row

    @staticmethod
    def _require_version(row: MangaRecord, row_version: int | None) -> None:
        if row_version is None:
            raise Conflict("缺少 row_version，请刷新后重试")
        if row.row_version != row_version:
            raise Conflict("记录已被其他进程修改，请刷新后重试")

    def _event(
        self,
        row: MangaRecord,
        operation: str,
        *,
        attempt_id: int | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        error_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            EventLog(
                manga_id=row.manga_id,
                attempt_id=attempt_id,
                component="web",
                event_type="manual",
                operation=operation,
                from_status=from_status,
                to_status=to_status,
                error_code=error_code,
                actor=self.actor,
                detail=detail or {},
            )
        )


def list_manga(
    session: Session,
    *,
    statuses: list[str] | None = None,
    query_text: str | None = None,
    queue_source: str | None = None,
    has_error: bool | None = None,
    limit: int = 50,
    cursor: str | None = None,
    offset: int = 0,
) -> MangaPage:
    limit = max(1, min(limit, 100))
    query = select(MangaRecord).order_by(
        desc(MangaRecord.posted_at).nulls_last(), MangaRecord.manga_id
    )
    if statuses:
        invalid = [value for value in statuses if value not in STATUS_LABELS]
        if invalid:
            raise InvalidRequest("无效状态：" + ", ".join(invalid))
        query = query.where(MangaRecord.status.in_(statuses))
    if queue_source:
        if queue_source not in {"automatic", "manual"}:
            raise InvalidRequest("无效队列来源")
        query = query.where(MangaRecord.queue_source == queue_source)
    if has_error is True:
        query = query.where(MangaRecord.last_error_at.is_not(None))
    elif has_error is False:
        query = query.where(MangaRecord.last_error_at.is_(None))
    if query_text and query_text.strip():
        query = query.where(_manga_search_predicate(query_text))
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        posted_at, manga_id = decoded
        if posted_at is None:
            query = query.where(
                and_(MangaRecord.posted_at.is_(None), MangaRecord.manga_id > manga_id)
            )
        else:
            query = query.where(
                or_(
                    MangaRecord.posted_at < posted_at,
                    MangaRecord.posted_at.is_(None),
                    and_(
                        MangaRecord.posted_at == posted_at,
                        MangaRecord.manga_id > manga_id,
                    ),
                )
            )
    elif offset:
        query = query.offset(max(0, offset))
    rows = list(session.scalars(query.limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1]) if has_more and rows else None
    return MangaPage(rows, next_cursor)


def list_review_manga(
    session: Session,
    *,
    status: str,
    query_text: str | None = None,
    error_code: str | None = None,
    operation: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> MangaPage:
    if status not in {Status.MANUAL_REVIEW.value, Status.QUARANTINED.value}:
        raise InvalidRequest("无效人工复核状态")
    limit = max(1, min(limit, 100))
    query = (
        select(MangaRecord)
        .where(MangaRecord.status == status)
        .order_by(desc(MangaRecord.status_updated_at), MangaRecord.manga_id)
    )
    if query_text and query_text.strip():
        query = query.where(_manga_search_predicate(query_text))
    if error_code:
        query = query.where(MangaRecord.last_error_code == error_code.strip())
    if operation:
        query = query.where(MangaRecord.last_error_operation == operation.strip())

    decoded = _decode_cursor(cursor)
    if decoded is not None:
        status_updated_at, manga_id = decoded
        if status_updated_at is None:
            raise InvalidRequest("无效人工复核分页游标")
        query = query.where(
            or_(
                MangaRecord.status_updated_at < status_updated_at,
                and_(
                    MangaRecord.status_updated_at == status_updated_at,
                    MangaRecord.manga_id > manga_id,
                ),
            )
        )

    rows = list(session.scalars(query.limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_datetime_cursor(rows[-1].status_updated_at, rows[-1].manga_id)
        if has_more and rows
        else None
    )
    return MangaPage(rows, next_cursor)


def review_facets(
    session: Session,
    *,
    status: str,
    query_text: str | None = None,
    operation: str | None = None,
) -> tuple[list[ReviewFacet], list[str]]:
    if status not in {Status.MANUAL_REVIEW.value, Status.QUARANTINED.value}:
        raise InvalidRequest("无效人工复核状态")
    conditions = [MangaRecord.status == status]
    if query_text and query_text.strip():
        conditions.append(_manga_search_predicate(query_text))
    if operation:
        conditions.append(MangaRecord.last_error_operation == operation.strip())

    facets = [
        ReviewFacet(str(code), int(count))
        for code, count in session.execute(
            select(MangaRecord.last_error_code, func.count())
            .where(*conditions, MangaRecord.last_error_code.is_not(None))
            .group_by(MangaRecord.last_error_code)
            .order_by(desc(func.count()), MangaRecord.last_error_code)
        )
    ]
    operations = list(
        session.scalars(
            select(MangaRecord.last_error_operation)
            .where(
                MangaRecord.status == status,
                MangaRecord.last_error_operation.is_not(None),
            )
            .distinct()
            .order_by(MangaRecord.last_error_operation)
        )
    )
    return facets, [str(value) for value in operations]


def _lease_expired(value: datetime | None, *, now: datetime | None = None) -> bool:
    if value is None:
        return False
    comparable_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    comparable_now = now or utcnow()
    if comparable_now.tzinfo is None:
        comparable_now = comparable_now.replace(tzinfo=UTC)
    else:
        comparable_now = comparable_now.astimezone(UTC)
    return comparable_value <= comparable_now


def conflict_rename_filename(manga_id: str, current_filename: str | None) -> str | None:
    """Build the operator-visible default used by the legacy conflict recovery."""

    if not current_filename:
        return None
    gallery_id, _separator, token = manga_id.partition("/")
    normal_prefix = f"[{gallery_id}] "
    prefix = normal_prefix
    if current_filename.startswith((normal_prefix, f"[{gallery_id}]")):
        safe_token = re.sub(r"[^A-Za-z0-9]", "", token)[:8] or "archive"
        prefix = f"[{gallery_id}-{safe_token}] "
    return safe_filename(prefix + current_filename)


def manga_detail(session: Session, manga_id: str) -> dict[str, Any]:
    row = session.scalar(
        select(MangaRecord)
        .where(MangaRecord.manga_id == manga_id)
        .options(selectinload(MangaRecord.info))
    )
    if row is None:
        raise NotFound("档案不存在")
    filename_matches: list[MangaRecord] = []
    duplicate_review = bool(
        row.last_error_code
        and row.last_error_code.casefold() == "lrr_409"
        or row.rename_target_filename
    )
    if duplicate_review and row.artifact_filename:
        filename_matches = list(
            session.scalars(
                select(MangaRecord)
                .where(
                    MangaRecord.manga_id != row.manga_id,
                    func.lower(MangaRecord.artifact_filename) == row.artifact_filename.lower(),
                )
                .order_by(desc(MangaRecord.status_updated_at), MangaRecord.manga_id)
                .limit(50)
            )
        )
    attempts = list(
        session.scalars(
            select(JobAttempt)
            .where(JobAttempt.manga_id == manga_id)
            .order_by(desc(JobAttempt.started_at))
            .limit(100)
        )
    )
    events = list(
        session.scalars(
            select(EventLog)
            .where(EventLog.manga_id == manga_id)
            .order_by(desc(EventLog.created_at), desc(EventLog.id))
            .limit(200)
        )
    )
    active_attempt = next(
        (attempt for attempt in attempts if attempt.id == row.active_attempt_id), None
    )
    expired_attempt = (
        active_attempt
        if active_attempt is not None
        and active_attempt.status == "running"
        and _lease_expired(row.lease_until)
        else None
    )
    return {
        "row": row,
        "attempts": attempts,
        "events": events,
        "filename_matches": filename_matches,
        "conflict_rename_available": (
            row.status == Status.MANUAL_REVIEW.value
            and duplicate_review
            and bool(row.artifact_location and row.artifact_filename)
        ),
        "conflict_rename_filename": (
            row.rename_target_filename
            or conflict_rename_filename(row.manga_id, row.artifact_filename)
        ),
        "active_attempt": active_attempt,
        "expired_attempt": expired_attempt,
        "progress_poll": bool(
            active_attempt is not None and active_attempt.operation == "direct_download"
        )
        or (
            row.download_method == "direct"
            and row.status in {Status.DOWNLOAD_PENDING.value, Status.DOWNLOADING.value}
        ),
    }


def dashboard_data(session: Session) -> dict[str, Any]:
    counts = dict(
        session.execute(select(MangaRecord.status, func.count()).group_by(MangaRecord.status)).all()
    )
    controls = {row.component: row for row in session.scalars(select(SystemControl))}
    health = {row.component: row for row in session.scalars(select(SystemHealth))}
    running = running_attempts(session)
    recent_errors = list(
        session.scalars(
            select(MangaRecord)
            .where(MangaRecord.last_error_at.is_not(None))
            .order_by(desc(MangaRecord.last_error_at))
            .limit(12)
        )
    )
    retries = list(
        session.scalars(
            select(MangaRecord)
            .where(MangaRecord.next_retry_at.is_not(None))
            .order_by(MangaRecord.next_retry_at)
            .limit(12)
        )
    )
    return {
        "counts": counts,
        "controls": controls,
        "health": health,
        "running": running,
        "recent_errors": recent_errors,
        "retries": retries,
    }


def running_attempts(session: Session, *, limit: int = 12) -> list[JobAttempt]:
    return list(
        session.scalars(
            select(JobAttempt)
            .where(JobAttempt.status == "running")
            .order_by(desc(JobAttempt.started_at))
            .limit(limit)
        )
    )


def manga_progress_data(session: Session, manga_id: str) -> dict[str, Any]:
    row = session.get(MangaRecord, manga_id)
    if row is None:
        raise NotFound("档案不存在")
    active_attempt = (
        session.get(JobAttempt, row.active_attempt_id) if row.active_attempt_id is not None else None
    )
    if active_attempt is not None and (
        active_attempt.manga_id != row.manga_id or active_attempt.status != "running"
    ):
        active_attempt = None
    direct_active = bool(
        active_attempt is not None and active_attempt.operation == "direct_download"
    )
    progress_poll = direct_active or (
        row.download_method == "direct"
        and row.status in {Status.DOWNLOAD_PENDING.value, Status.DOWNLOADING.value}
    )
    return {
        "row": row,
        "active_attempt": active_attempt if direct_active else None,
        "progress_poll": progress_poll,
    }


def list_events(
    session: Session,
    *,
    manga_id: str | None = None,
    component: str | None = None,
    operation: str | None = None,
    error_only: bool = False,
    limit: int = 100,
) -> list[EventLog]:
    query = select(EventLog).order_by(desc(EventLog.created_at), desc(EventLog.id))
    if manga_id:
        query = query.where(EventLog.manga_id == manga_id.strip())
    if component:
        query = query.where(EventLog.component == component.strip())
    if operation:
        query = query.where(EventLog.operation == operation.strip())
    if error_only:
        query = query.where(EventLog.error_code.is_not(None))
    return list(session.scalars(query.limit(max(1, min(limit, 500)))))


def allowed_actions(status: str) -> list[dict[str, str]]:
    return [
        {"name": action, "label": ACTION_LABELS[action]}
        for action, states in _ACTION_EVENTS.items()
        if status in states
    ]


def serialize_manga(row: MangaRecord) -> dict[str, Any]:
    keys = (
        "manga_id",
        "name",
        "real_name",
        "link",
        "torrent_link",
        "posted_at",
        "category",
        "tags_raw",
        "pages",
        "rating",
        "uploader",
        "remark",
        "queue_source",
        "status",
        "screen_pending",
        "screen_group_id",
        "priority",
        "download_method",
        "defer_until",
        "attempt_count",
        "next_retry_at",
        "lease_owner",
        "lease_until",
        "active_attempt_id",
        "last_error_operation",
        "last_error_code",
        "last_error_detail",
        "last_error_at",
        "superseded_by_id",
        "external_download_id",
        "artifact_location",
        "artifact_filename",
        "rename_target_filename",
        "artifact_kind",
        "artifact_generation",
        "artifact_size",
        "artifact_sha1",
        "artifact_checked_at",
        "lrr_archive_id",
        "row_version",
        "status_updated_at",
        "created_at",
        "updated_at",
    )
    value = {key: getattr(row, key) for key in keys}
    value["allowed_actions"] = allowed_actions(row.status)
    return value


def serialize_model(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def safe_detail(value: Any) -> Any:
    blocked = ("authorization", "cookie", "password", "secret", "proxy")
    if isinstance(value, dict):
        return {
            str(key): "[已隐藏]"
            if any(term in str(key).casefold() for term in blocked)
            else safe_detail(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [safe_detail(item) for item in value]
    return value


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _manga_search_predicate(value: str):
    value = value.strip()
    pattern = f"%{_escape_like(value.casefold())}%"
    return or_(
        MangaRecord.manga_id.ilike(f"{_escape_like(value)}%", escape="\\"),
        func.lower(MangaRecord.name).like(pattern, escape="\\"),
        func.lower(MangaRecord.real_name).like(pattern, escape="\\"),
        func.lower(MangaRecord.artifact_filename).like(pattern, escape="\\"),
    )


def _encode_cursor(row: MangaRecord) -> str:
    return _encode_datetime_cursor(row.posted_at, row.manga_id)


def _encode_datetime_cursor(value: datetime | None, manga_id: str) -> str:
    payload = json.dumps(
        [value.isoformat() if value is not None else None, manga_id],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(value: str | None) -> tuple[datetime | None, str] | None:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        posted_at, manga_id = json.loads(base64.urlsafe_b64decode(value + padding))
        parsed_posted_at = datetime.fromisoformat(str(posted_at)) if posted_at is not None else None
        return parsed_posted_at, str(manga_id)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise InvalidRequest("无效分页游标") from None
