from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


STATUS_VALUES = (
    "discovered",
    "deferred",
    "download_pending",
    "downloading",
    "downloaded",
    "validating",
    "preparing",
    "upload_pending",
    "uploading",
    "uploaded",
    "completed",
    "quarantined",
    "manual_review",
    "special_processing",
    "skipped",
    "unavailable",
    "outdated",
    "force_delete_pending",
    "rename_pending",
    "deleted",
    "cancel_requested",
    "cancelled",
)
DOWNLOAD_METHOD_VALUES = ("torrent", "direct", "hah", "aria2")
JSON_OBJECT = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MangaRecord(Base):
    __tablename__ = "manga"
    __table_args__ = (
        CheckConstraint("queue_source IN ('automatic', 'manual')", name="ck_manga_queue_source"),
        CheckConstraint(
            "status IN (" + ",".join(repr(v) for v in STATUS_VALUES) + ")", name="ck_manga_status"
        ),
        CheckConstraint(
            "download_method IS NULL OR download_method IN ("
            + ",".join(repr(v) for v in DOWNLOAD_METHOD_VALUES)
            + ")",
            name="ck_manga_download_method",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_manga_attempt_count"),
        CheckConstraint(
            "artifact_generation IS NULL OR artifact_generation >= 1", name="ck_manga_generation"
        ),
        CheckConstraint(
            "artifact_kind IS NULL OR artifact_kind IN ('file', 'directory', 'zip')",
            name="ck_manga_artifact_kind",
        ),
        CheckConstraint(
            "artifact_location IS NULL OR artifact_location IN ('torrent_download', 'hah_download', 'direct_download', 'aria2_download', 'prepared', 'quarantine', 'trash')",
            name="ck_manga_artifact_location",
        ),
        CheckConstraint(
            "lrr_archive_id IS NULL OR lrr_archive_id ~ '^[0-9A-Fa-f]{40}$'",
            name="ck_manga_lrr_archive_id",
        ),
        ForeignKeyConstraint(
            ["manga_id", "active_attempt_id"],
            ["job_attempt.manga_id", "job_attempt.id"],
            name="fk_manga_active_attempt",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_manga_queue", "status", "priority", "next_retry_at", "created_at"),
        Index("ix_manga_lease_until", "lease_until"),
        Index("ix_manga_external_download_id", "external_download_id"),
        Index("ix_manga_lrr_archive_id", "lrr_archive_id"),
        Index("ix_manga_screen_pending", "screen_pending", "status"),
    )

    manga_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")
    real_name: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(Text, default="")
    torrent_link: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    category: Mapped[str] = mapped_column(Text, default="")
    tags_raw: Mapped[str] = mapped_column(Text, default="")
    pages: Mapped[int | None] = mapped_column(Integer)
    rating: Mapped[int | None] = mapped_column(Integer)
    uploader: Mapped[str] = mapped_column(Text, default="")
    remark: Mapped[str | None] = mapped_column(Text)
    source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queue_source: Mapped[str] = mapped_column(String(16), default="automatic")

    status: Mapped[str] = mapped_column(String(32), default="discovered", nullable=False)
    # ``screen_pending`` is the explicit replacement for legacy autostate=1.
    # A discovered row with this flag set participates in screenall; a plain
    # discovered row was legacy state=1/autostate=NULL and is only recorded.
    screen_pending: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    screen_group_id: Mapped[str | None] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    download_method: Mapped[str | None] = mapped_column(String(16))
    defer_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_attempt_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error_operation: Mapped[str | None] = mapped_column(String(24))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("manga.manga_id", ondelete="RESTRICT")
    )
    row_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    external_download_id: Mapped[str | None] = mapped_column(Text)
    artifact_location: Mapped[str | None] = mapped_column(String(32))
    artifact_filename: Mapped[str | None] = mapped_column(Text)
    rename_target_filename: Mapped[str | None] = mapped_column(Text)
    artifact_kind: Mapped[str | None] = mapped_column(String(16))
    artifact_generation: Mapped[int | None] = mapped_column(Integer)
    artifact_size: Mapped[int | None] = mapped_column(BigInteger)
    artifact_sha1: Mapped[str | None] = mapped_column(String(40))
    artifact_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lrr_archive_id: Mapped[str | None] = mapped_column(String(63))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    status_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    attempts: Mapped[list[JobAttempt]] = relationship(
        "JobAttempt", back_populates="manga", foreign_keys="JobAttempt.manga_id"
    )
    info: Mapped[MangaInfoRecord | None] = relationship(
        "MangaInfoRecord", back_populates="manga", uselist=False
    )
    special_workflows: Mapped[list[SpecialWorkflow]] = relationship(
        "SpecialWorkflow", back_populates="manga", foreign_keys="SpecialWorkflow.manga_id"
    )


class MangaInfoRecord(Base):
    __tablename__ = "mangainfo"
    manga_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("manga.manga_id", ondelete="RESTRICT"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, default="")
    roman_name: Mapped[str] = mapped_column(Text, default="")
    real_name: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(Text, default="")
    uploader: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language: Mapped[str] = mapped_column(Text, default="")
    estimated_size_raw: Mapped[str] = mapped_column(Text, default="")
    pages: Mapped[int | None] = mapped_column(Integer)
    favorited: Mapped[int | None] = mapped_column(Integer)
    rating_count: Mapped[int | None] = mapped_column(Integer)
    rating: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tags_raw: Mapped[str] = mapped_column(Text, default="")
    tags_translated_raw: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    manga: Mapped[MangaRecord] = relationship("MangaRecord", back_populates="info")


class JobAttempt(Base):
    __tablename__ = "job_attempt"
    __table_args__ = (
        UniqueConstraint("manga_id", "operation", "attempt_no", name="uq_attempt_operation_no"),
        UniqueConstraint("manga_id", "id", name="uq_attempt_manga_id_id"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'abandoned')", name="ck_attempt_status"
        ),
        Index("ix_attempt_operation_status", "operation", "status", "started_at"),
        Index("ix_attempt_manga_started", "manga_id", "started_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manga_id: Mapped[str | None] = mapped_column(
        String(100), ForeignKey("manga.manga_id", ondelete="RESTRICT")
    )
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(16), default="supervisor", nullable=False)
    actor: Mapped[str] = mapped_column(Text, default="")
    previous_status: Mapped[str | None] = mapped_column(String(32))
    resulting_status: Mapped[str | None] = mapped_column(String(32))
    lease_token: Mapped[str] = mapped_column(String(36), nullable=False)
    artifact_generation: Mapped[int | None] = mapped_column(Integer)
    external_task_id: Mapped[str | None] = mapped_column(Text)
    external_effect_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress_bytes: Mapped[int | None] = mapped_column(BigInteger)
    progress_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    progress_speed_bps: Mapped[float | None] = mapped_column(Float)
    progress_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    manga: Mapped[MangaRecord | None] = relationship(
        "MangaRecord", back_populates="attempts", foreign_keys=[manga_id]
    )


class SpecialWorkflow(Base):
    __tablename__ = "special_workflow"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed', 'failed', 'cancelled')",
            name="ck_special_workflow_status",
        ),
        CheckConstraint("row_version >= 0", name="ck_special_workflow_row_version"),
        Index(
            "uq_special_workflow_active_manga",
            "manga_id",
            unique=True,
            postgresql_where=sql_text("status = 'active'"),
            sqlite_where=sql_text("status = 'active'"),
        ),
        Index("ix_special_workflow_status_updated", "status", "updated_at"),
        Index("ix_special_workflow_kind_phase", "kind", "status", "phase"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manga_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("manga.manga_id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    resume_status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_OBJECT, default=dict, nullable=False)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON_OBJECT, default=dict, nullable=False)
    row_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    manga: Mapped[MangaRecord] = relationship(
        "MangaRecord", back_populates="special_workflows", foreign_keys=[manga_id]
    )
    jobs: Mapped[list[SpecialJob]] = relationship(
        "SpecialJob", back_populates="workflow", foreign_keys="SpecialJob.workflow_id"
    )


class SpecialJob(Base):
    __tablename__ = "special_job"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "operation", "attempt_no", name="uq_special_job_operation_no"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'abandoned', 'cancelled')",
            name="ck_special_job_status",
        ),
        CheckConstraint(
            "trigger_source IN ('web', 'cli', 'system')",
            name="ck_special_job_trigger_source",
        ),
        CheckConstraint("attempt_no >= 1", name="ck_special_job_attempt_no"),
        Index("ix_special_job_queue", "status", "next_run_at", "created_at"),
        Index("ix_special_job_workflow_created", "workflow_id", "created_at"),
        Index(
            "uq_special_job_running_workflow",
            "workflow_id",
            unique=True,
            postgresql_where=sql_text("status = 'running'"),
            sqlite_where=sql_text("status = 'running'"),
        ),
        Index(
            "uq_special_job_active_operation",
            "workflow_id",
            "operation",
            unique=True,
            postgresql_where=sql_text("status IN ('queued', 'running')"),
            sqlite_where=sql_text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("special_workflow.id", ondelete="RESTRICT"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[dict[str, Any]] = mapped_column(JSON_OBJECT, default=dict, nullable=False)
    external_effect_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflow: Mapped[SpecialWorkflow] = relationship(
        "SpecialWorkflow", back_populates="jobs", foreign_keys=[workflow_id]
    )


class EventLog(Base):
    __tablename__ = "event_log"
    __table_args__ = (
        Index("ix_event_manga_created", "manga_id", "created_at"),
        Index("ix_event_component_created", "component", "created_at"),
        Index("ix_event_run_created", "run_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manga_id: Mapped[str | None] = mapped_column(
        String(100), ForeignKey("manga.manga_id", ondelete="RESTRICT")
    )
    attempt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("job_attempt.id", ondelete="RESTRICT")
    )
    run_id: Mapped[str | None] = mapped_column(String(36))
    component: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str | None] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    actor: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class SystemControl(Base):
    __tablename__ = "system_control"
    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'paused', 'draining')",
            name="ck_system_control_state",
        ),
    )
    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(Text, default="system")
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class SystemHealth(Base):
    __tablename__ = "system_health"
    __table_args__ = (
        CheckConstraint(
            "status IN ('healthy', 'degraded', 'unavailable', 'unknown')",
            name="ck_system_health_status",
        ),
        Index("ix_system_health_checked", "checked_at"),
    )

    component: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
