from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, func, inspect, or_, select, text
from sqlalchemy.orm import Session, selectinload

from ..config.loader import SUPERVISOR_MODULES
from ..db.models import EventLog, JobAttempt, MangaRecord, SystemControl, SystemHealth
from ..db.repository import utcnow
from ..domain.states import Status, can_transition, transition_target

CONTROL_COMPONENTS = ("supervisor", *SUPERVISOR_MODULES)

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
    "delete": "过时删除",
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


class WebService:
    def __init__(self, session: Session, *, actor: str) -> None:
        self.session = session
        self.actor = actor

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
        from_status: str | None = None,
        to_status: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            EventLog(
                manga_id=row.manga_id,
                component="web",
                event_type="manual",
                operation=operation,
                from_status=from_status,
                to_status=to_status,
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
        value = query_text.strip()
        pattern = f"%{_escape_like(value.casefold())}%"
        query = query.where(
            or_(
                MangaRecord.manga_id.ilike(f"{_escape_like(value)}%", escape="\\"),
                func.lower(MangaRecord.name).like(pattern, escape="\\"),
                func.lower(MangaRecord.real_name).like(pattern, escape="\\"),
            )
        )
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


def manga_detail(session: Session, manga_id: str) -> dict[str, Any]:
    row = session.scalar(
        select(MangaRecord)
        .where(MangaRecord.manga_id == manga_id)
        .options(selectinload(MangaRecord.info))
    )
    if row is None:
        raise NotFound("档案不存在")
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
    return {"row": row, "attempts": attempts, "events": events}


def dashboard_data(session: Session) -> dict[str, Any]:
    counts = dict(
        session.execute(select(MangaRecord.status, func.count()).group_by(MangaRecord.status)).all()
    )
    controls = {row.component: row for row in session.scalars(select(SystemControl))}
    health = {row.component: row for row in session.scalars(select(SystemHealth))}
    running = list(
        session.scalars(
            select(JobAttempt)
            .where(JobAttempt.status == "running")
            .order_by(desc(JobAttempt.started_at))
            .limit(12)
        )
    )
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
    version = None
    connection = session.connection()
    if inspect(connection).has_table("alembic_version"):
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return {
        "counts": counts,
        "controls": controls,
        "health": health,
        "running": running,
        "recent_errors": recent_errors,
        "retries": retries,
        "migration_version": version,
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


def _encode_cursor(row: MangaRecord) -> str:
    payload = json.dumps(
        [row.posted_at.isoformat() if row.posted_at is not None else None, row.manga_id],
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
