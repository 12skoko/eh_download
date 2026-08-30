from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, desc, exists, func, or_, select, update
from sqlalchemy.orm import Session

from ..domain.models import MangaInfo
from ..domain.states import (
    REPLACEMENT_DELETE_READY_STATUSES,
    TRANSITIONS,
    Status,
    can_transition,
    transition_target,
)
from .models import EventLog, JobAttempt, MangaInfoRecord, MangaRecord, SystemControl


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ClaimedAttempt:
    manga_id: str
    attempt_id: int
    operation: str
    lease_token: str
    artifact_generation: int | None


OPERATION_STATES: dict[str, tuple[tuple[str, ...], str | None]] = {
    "screen": ((Status.DISCOVERED.value,), None),
    "details": (
        (
            Status.DOWNLOAD_PENDING.value,
            Status.DOWNLOADING.value,
            Status.DOWNLOADED.value,
            Status.VALIDATING.value,
            Status.UPLOAD_PENDING.value,
        ),
        None,
    ),
    "torrent_download": (
        (Status.DOWNLOAD_PENDING.value, Status.DOWNLOADING.value),
        Status.DOWNLOADING.value,
    ),
    "direct_download": (
        (Status.DOWNLOAD_PENDING.value, Status.DOWNLOADING.value),
        Status.DOWNLOADING.value,
    ),
    "validate": (
        (
            Status.DOWNLOADED.value,
            Status.RENAME_PENDING.value,
            Status.VALIDATING.value,
        ),
        Status.VALIDATING.value,
    ),
    "prepare": ((Status.PREPARING.value,), Status.PREPARING.value),
    "upload": ((Status.UPLOAD_PENDING.value,), Status.UPLOADING.value),
    "cleanup": ((Status.UPLOADED.value,), Status.UPLOADED.value),
    # Keep the queued status unchanged so the worker can distinguish ordinary
    # replacement deletion from the Web-only forced deletion path.
    "delete": (
        (Status.OUTDATED.value, Status.FORCE_DELETE_PENDING.value),
        None,
    ),
}


def _details_missing_clause():
    """Match rows whose persisted MangaInfo is not ready for upload."""

    complete = and_(
        MangaInfoRecord.name != "",
        MangaInfoRecord.link != "",
        MangaInfoRecord.category != "",
        MangaInfoRecord.uploader != "",
        MangaInfoRecord.language != "",
        MangaInfoRecord.estimated_size_raw != "",
        MangaInfoRecord.posted_at.is_not(None),
        MangaInfoRecord.pages.is_not(None),
        MangaInfoRecord.tags_raw.is_not(None),
    )
    return ~exists().where(
        MangaInfoRecord.manga_id == MangaRecord.manga_id,
        complete,
    )


def _delete_ready_clause():
    """Keep ordinary outdated rows queued until their replacement is underway."""

    replacement = MangaRecord.__table__.alias("replacement")
    replacement_ready = exists(
        select(1)
        .select_from(replacement)
        .where(
            replacement.c.manga_id == MangaRecord.superseded_by_id,
            replacement.c.status.in_(REPLACEMENT_DELETE_READY_STATUSES),
        )
    )
    return or_(
        MangaRecord.status == Status.FORCE_DELETE_PENDING.value,
        and_(MangaRecord.status == Status.OUTDATED.value, replacement_ready),
    )


def _retry_ready_clause(operation: str, now: datetime):
    ready = or_(MangaRecord.next_retry_at.is_(None), MangaRecord.next_retry_at <= now)
    if operation == "details":
        return or_(
            ready,
            MangaRecord.last_error_operation.is_(None),
            MangaRecord.last_error_operation != "details",
        )
    if operation == "torrent_download":
        # A details backoff must not stop qBittorrent from being submitted or
        # polled. Other operation backoffs remain authoritative.
        return or_(ready, MangaRecord.last_error_operation == "details")
    return ready


class ArchiveRepository:
    def __init__(self, session: Session, *, run_id: str | None = None) -> None:
        self.session = session
        self.run_id = run_id
        self.run_actor: str | None = None

    def start_collect_run(
        self,
        *,
        trigger_source: str,
        detail: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> str:
        """Attach one UUID to every event emitted by a complete collect."""

        if self.run_id is not None:
            raise ValueError("repository is already attached to a run")
        self.run_id = run_id or str(uuid.uuid4())
        self.run_actor = trigger_source
        self._event(
            None,
            "collect_started",
            actor=trigger_source,
            operation="collect",
            detail={**(detail or {}), "run_id": self.run_id},
        )
        self.session.flush()
        return self.run_id

    def finish_collect_run(
        self,
        status: str = "succeeded",
        *,
        detail: dict[str, Any] | None = None,
    ) -> str | None:
        if self.run_id is None:
            return None
        self._event(
            None,
            "collect_finished",
            actor=self.run_actor or "collector",
            operation="collect",
            detail={**(detail or {}), "run_id": self.run_id, "status": status},
        )
        self.session.flush()
        return self.run_id

    def get(self, manga_id: str) -> MangaRecord | None:
        return self.session.get(MangaRecord, manga_id)

    def automatic_collect_end(
        self,
        *,
        days: int = 6,
        offset: int = 3000,
        now: datetime | None = None,
    ) -> int:
        if days < 0:
            raise ValueError("collect end days must not be negative")
        if offset < 0:
            raise ValueError("collect end offset must not be negative")
        cutoff = (now or utcnow()) - timedelta(days=days)
        query = (
            select(MangaRecord.manga_id)
            .where(
                MangaRecord.status == Status.DEFERRED.value,
                MangaRecord.queue_source == "automatic",
                MangaRecord.posted_at.is_not(None),
                MangaRecord.posted_at > cutoff,
                ~MangaRecord.manga_id.like("picacg/%"),
            )
            .order_by(MangaRecord.posted_at.asc())
        )
        for manga_id in self.session.scalars(query):
            gallery_id, separator, _token = manga_id.partition("/")
            if separator and gallery_id.isdecimal():
                return int(gallery_id) - offset
        return 0

    def upsert_manga(self, record: MangaRecord, *, actor: str = "collector") -> MangaRecord:
        current = self.session.get(MangaRecord, record.manga_id)
        if current is None:
            self.session.add(record)
            self.session.flush()
            self._event(
                record.manga_id,
                "status_changed",
                actor=actor,
                to_status=record.status,
                detail={"reason": "discovered"},
            )
            if self.run_id is not None:
                self._event(
                    record.manga_id,
                    "collect_touched",
                    actor=actor,
                    operation="collect",
                    to_status=record.status,
                    detail={"created": True},
                )
            return record
        previous_status = current.status
        # Collection must not overwrite operator controls or a running attempt.
        for field in (
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
            "source_fetched_at",
        ):
            setattr(current, field, getattr(record, field))
        if current.status == Status.DEFERRED.value:
            previous = current.status
            current.status = record.status
            current.remark = record.remark
            current.defer_until = record.defer_until
            current.screen_group_id = None
            current.updated_at = utcnow()
            current.row_version += 1
            if current.status != previous:
                current.status_updated_at = current.updated_at
                self._event(
                    current.manga_id,
                    "status_changed",
                    actor=actor,
                    from_status=previous,
                    to_status=current.status,
                    detail={"reason": "deferred_reclassified"},
                )
        if current.status in (
            Status.DISCOVERED.value,
            Status.DEFERRED.value,
            Status.FILTERED_OUT.value,
            Status.SKIPPED.value,
        ):
            current.updated_at = utcnow()
        self.session.flush()
        if self.run_id is not None:
            self._event(
                current.manga_id,
                "collect_touched",
                actor=actor,
                operation="collect",
                from_status=previous_status,
                to_status=current.status,
                detail={"created": False},
            )
        return current

    def upsert_info(self, info: MangaInfo, *, actor: str = "details") -> MangaInfoRecord:
        record = self.session.get(MangaInfoRecord, info.manga_id)
        if record is None:
            record = MangaInfoRecord(manga_id=info.manga_id)
            self.session.add(record)
        fetched_at = utcnow()
        info.fetched_at = fetched_at
        values = {
            "name": info.name,
            "roman_name": info.roman_name,
            "real_name": info.real_name,
            "link": info.link,
            "category": info.category,
            "uploader": info.uploader,
            "posted_at": info.posted_at,
            "language": info.language,
            "estimated_size_raw": info.estimated_size_raw,
            "pages": info.pages,
            "favorited": info.favorited,
            "rating_count": info.rating_count,
            "rating": info.rating,
            "fetched_at": fetched_at,
            "tags_raw": info.tags_raw,
            "tags_translated_raw": info.tags_translated_raw,
        }
        for key, value in values.items():
            setattr(record, key, value)
        self.session.flush()
        self._event(
            info.manga_id, "details_upserted", actor=actor, detail={"complete": info.is_complete()}
        )
        return record

    def mark_parent_outdated(
        self, parent_id: str | None, replacement_id: str, *, actor: str = "details"
    ) -> int:
        if not parent_id:
            return 0
        rows = list(
            self.session.scalars(
                select(MangaRecord).where(
                    MangaRecord.manga_id.like(f"{parent_id}/%"),
                    MangaRecord.manga_id != replacement_id,
                    MangaRecord.status.in_((Status.UPLOADED.value, Status.COMPLETED.value)),
                )
            )
        )
        for row in rows:
            if row.status == Status.OUTDATED.value and row.superseded_by_id == replacement_id:
                continue
            previous = row.status
            row.status, row.superseded_by_id, row.row_version = (
                Status.OUTDATED.value,
                replacement_id,
                row.row_version + 1,
            )
            row.status_updated_at = row.updated_at = utcnow()
            self._event(
                row.manga_id,
                "status_changed",
                actor=actor,
                from_status=previous,
                to_status=Status.OUTDATED.value,
                detail={"superseded_by_id": replacement_id},
            )
        return len(rows)

    def list_queue(self, statuses: Iterable[Status | str], limit: int = 100) -> list[MangaRecord]:
        values = [Status(s).value for s in statuses]
        query = (
            select(MangaRecord)
            .where(MangaRecord.status.in_(values))
            .where(_retry_ready_clause("queue", utcnow()))
            .order_by(desc(MangaRecord.priority), MangaRecord.created_at)
            .limit(limit)
        )
        return list(self.session.scalars(query))

    def list_screen_candidates(self, *, limit: int = 100) -> list[MangaRecord]:
        return list(
            self.session.scalars(
                select(MangaRecord)
                .where(
                    MangaRecord.status == Status.DISCOVERED.value,
                    ~MangaRecord.manga_id.like("picacg/%"),
                )
                .order_by(desc(MangaRecord.priority), MangaRecord.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )

    def list_screen_group(self, real_name: str) -> list[MangaRecord]:
        return list(
            self.session.scalars(
                select(MangaRecord)
                .where(
                    MangaRecord.real_name == real_name,
                    MangaRecord.category.in_(("Manga", "Doujinshi")),
                )
                .order_by(MangaRecord.created_at, MangaRecord.manga_id)
                .with_for_update()
            )
        )

    def apply_screen_outcome(
        self,
        row: MangaRecord,
        *,
        status: Status,
        reason: str,
        actor: str = "screen",
        screen_group_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if row.status != Status.DISCOVERED.value:
            return
        previous = row.status
        now = utcnow()
        row.status = status.value
        row.remark = reason
        row.defer_until = None
        row.screen_group_id = screen_group_id
        row.status_updated_at = row.updated_at = now
        row.row_version += 1
        event_detail = {"reason": reason}
        if detail:
            event_detail.update(detail)
        self._event(
            row.manga_id,
            "status_changed",
            actor=actor,
            operation="screen",
            from_status=previous,
            to_status=row.status,
            detail=event_detail,
        )

    def complete_cancellations(self, *, now: datetime | None = None, limit: int = 100) -> int:
        now = now or utcnow()
        rows = list(
            self.session.scalars(
                select(MangaRecord)
                .where(
                    MangaRecord.status == Status.CANCEL_REQUESTED.value,
                    MangaRecord.active_attempt_id.is_(None),
                )
                .order_by(MangaRecord.status_updated_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        for row in rows:
            previous = row.status
            row.status = Status.CANCELLED.value
            row.status_updated_at = row.updated_at = now
            row.row_version += 1
            self._event(
                row.manga_id,
                "status_changed",
                actor="supervisor",
                from_status=previous,
                to_status=row.status,
                detail={"reason": "cancel_request_completed"},
            )
        return len(rows)

    def has_work(self, operation: str, *, large_upload_threshold_bytes: int = 0) -> bool:
        try:
            states, _ = OPERATION_STATES[operation]
        except KeyError as exc:
            raise ValueError(f"Unsupported operation: {operation}") from exc
        now = utcnow()
        query = (
            select(MangaRecord.manga_id)
            .where(MangaRecord.status.in_(states))
            .where(_retry_ready_clause(operation, now))
            # Expired attempts require manual inspection; only unleased rows
            # are eligible for automatic scheduling.
            .where(MangaRecord.lease_until.is_(None))
            .limit(1)
        )
        if operation == "details":
            query = query.where(_details_missing_clause())
        elif operation == "torrent_download":
            query = query.where(
                or_(MangaRecord.download_method.is_(None), MangaRecord.download_method == "torrent")
            )
        elif operation == "direct_download":
            query = query.where(
                or_(
                    MangaRecord.download_method.in_(("direct", "hah", "aria2")),
                    and_(MangaRecord.download_method.is_(None), MangaRecord.torrent_link == ""),
                )
            )
        elif operation == "upload" and large_upload_threshold_bytes > 0:
            query = query.where(
                or_(
                    MangaRecord.artifact_size.is_(None),
                    MangaRecord.artifact_size < large_upload_threshold_bytes,
                )
            )
        elif operation == "delete":
            query = query.where(_delete_ready_clause())
        return self.session.scalar(query) is not None

    def claim_next(
        self,
        operation: str,
        *,
        owner: str,
        lease_seconds: int = 900,
        actor: str | None = None,
        large_upload_threshold_bytes: int = 0,
    ) -> ClaimedAttempt | None:
        try:
            states, execution_state = OPERATION_STATES[operation]
        except KeyError as exc:
            raise ValueError(f"Unsupported operation: {operation}") from exc
        now = utcnow()
        query = (
            select(MangaRecord)
            .where(MangaRecord.status.in_(states))
            .where(_retry_ready_clause(operation, now))
            .where(MangaRecord.lease_until.is_(None))
            .order_by(desc(MangaRecord.priority), MangaRecord.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if operation == "details":
            query = query.where(_details_missing_clause())
        elif operation == "torrent_download":
            query = query.where(
                or_(MangaRecord.download_method.is_(None), MangaRecord.download_method == "torrent")
            )
        elif operation == "direct_download":
            query = query.where(
                or_(
                    MangaRecord.download_method.in_(("direct", "hah", "aria2")),
                    and_(MangaRecord.download_method.is_(None), MangaRecord.torrent_link == ""),
                )
            )
        elif operation == "upload" and large_upload_threshold_bytes > 0:
            query = query.where(
                or_(
                    MangaRecord.artifact_size.is_(None),
                    MangaRecord.artifact_size < large_upload_threshold_bytes,
                )
            )
        elif operation == "delete":
            query = query.where(_delete_ready_clause())
        manga = self.session.scalars(query).first()
        if manga is None:
            return None
        attempt_no = (
            self.session.scalar(
                select(func.coalesce(func.max(JobAttempt.attempt_no), 0) + 1).where(
                    JobAttempt.manga_id == manga.manga_id, JobAttempt.operation == operation
                )
            )
            or 1
        )
        token = str(uuid.uuid4())
        attempt = JobAttempt(
            manga_id=manga.manga_id,
            operation=operation,
            attempt_no=int(attempt_no),
            trigger_source="supervisor",
            actor=actor or owner,
            previous_status=manga.status,
            resulting_status=None,
            lease_token=token,
            artifact_generation=manga.artifact_generation,
        )
        self.session.add(attempt)
        self.session.flush()
        manga.active_attempt_id = attempt.id
        manga.lease_token = token
        manga.lease_owner = owner
        manga.lease_until = now + timedelta(seconds=lease_seconds)
        manga.attempt_count += 1
        if execution_state:
            manga.status = execution_state
            manga.status_updated_at = now
        manga.row_version += 1
        self.session.flush()
        self._event(
            manga.manga_id,
            "lease_acquired",
            actor=actor or owner,
            attempt_id=attempt.id,
            operation=operation,
            from_status=attempt.previous_status,
            to_status=manga.status,
            detail={"lease_owner": owner},
        )
        return ClaimedAttempt(
            manga.manga_id, attempt.id, operation, token, manga.artifact_generation
        )

    def renew(self, claim: ClaimedAttempt, *, owner: str, lease_seconds: int) -> bool:
        with self.session.no_autoflush:
            result = self.session.execute(
                update(MangaRecord)
                .where(
                    MangaRecord.manga_id == claim.manga_id,
                    MangaRecord.active_attempt_id == claim.attempt_id,
                    MangaRecord.lease_token == claim.lease_token,
                    MangaRecord.lease_owner == owner,
                )
                .values(lease_until=utcnow() + timedelta(seconds=lease_seconds))
            )
        return result.rowcount == 1

    def fenced(
        self, claim: ClaimedAttempt, *, owner: str, require_generation: bool = False
    ) -> MangaRecord | None:
        conditions = [
            MangaRecord.manga_id == claim.manga_id,
            MangaRecord.active_attempt_id == claim.attempt_id,
            MangaRecord.lease_token == claim.lease_token,
            MangaRecord.lease_owner == owner,
        ]
        if require_generation:
            if claim.artifact_generation is None:
                conditions.append(MangaRecord.artifact_generation.is_(None))
            else:
                conditions.append(MangaRecord.artifact_generation == claim.artifact_generation)
        # A stale worker may have changed the ORM object before reaching this
        # check. Do not autoflush those changes before the fencing predicate
        # is evaluated.
        with self.session.no_autoflush:
            row = self.session.execute(
                select(
                    MangaRecord.manga_id,
                    MangaRecord.status,
                    MangaRecord.active_attempt_id,
                    MangaRecord.lease_token,
                    MangaRecord.lease_owner,
                    MangaRecord.lease_until,
                    MangaRecord.row_version,
                ).where(and_(*conditions))
            ).first()
            if row is None:
                return None
            manga = self.session.get(MangaRecord, claim.manga_id)
            if manga is None:
                return None
            # Refresh only fencing/status columns. Task code may have already
            # staged a new artifact fingerprint that must survive this read.
            manga.status = row.status
            manga.active_attempt_id = row.active_attempt_id
            manga.lease_token = row.lease_token
            manga.lease_owner = row.lease_owner
            manga.lease_until = row.lease_until
            manga.row_version = row.row_version
            return manga

    def begin_external_effect(self, claim: ClaimedAttempt, *, owner: str) -> bool:
        if self.fenced(claim, owner=owner) is None:
            return False
        now = utcnow()
        result = self.session.execute(
            update(JobAttempt)
            .where(
                JobAttempt.id == claim.attempt_id,
                JobAttempt.manga_id == claim.manga_id,
                JobAttempt.lease_token == claim.lease_token,
                JobAttempt.status == "running",
            )
            .values(external_effect_started_at=now)
        )
        return result.rowcount == 1

    def set_external_id(
        self, claim: ClaimedAttempt, external_id: str, *, owner: str | None = None
    ) -> bool:
        if owner is not None and self.fenced(claim, owner=owner) is None:
            return False
        result = self.session.execute(
            update(JobAttempt)
            .where(
                JobAttempt.id == claim.attempt_id,
                JobAttempt.manga_id == claim.manga_id,
                JobAttempt.lease_token == claim.lease_token,
            )
            .values(external_task_id=external_id)
        )
        return result.rowcount == 1

    def update_attempt_progress(
        self,
        claim: ClaimedAttempt,
        *,
        downloaded_bytes: int,
        total_bytes: int | None,
        speed_bps: float | None,
    ) -> bool:
        """Persist an observable snapshot without advancing the workflow."""

        result = self.session.execute(
            update(JobAttempt)
            .where(
                JobAttempt.id == claim.attempt_id,
                JobAttempt.manga_id == claim.manga_id,
                JobAttempt.lease_token == claim.lease_token,
                JobAttempt.status == "running",
            )
            .values(
                progress_bytes=max(0, downloaded_bytes),
                progress_total_bytes=(max(0, total_bytes) if total_bytes is not None else None),
                progress_speed_bps=(max(0.0, speed_bps) if speed_bps is not None else None),
                progress_updated_at=utcnow(),
            )
        )
        return result.rowcount == 1

    def finish(
        self,
        claim: ClaimedAttempt,
        *,
        owner: str,
        event: str | None = None,
        status: Status | str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        manga = self.fenced(claim, owner=owner)
        with self.session.no_autoflush:
            attempt = self.session.get(JobAttempt, claim.attempt_id)
        if manga is None or attempt is None or attempt.status != "running":
            if manga is None:
                with self.session.no_autoflush:
                    stale = self.session.get(MangaRecord, claim.manga_id)
                if stale is not None:
                    # Discard dirty state produced by the expired worker and
                    # reload the row owned by the current attempt.
                    self.session.refresh(stale)
            if attempt is not None and attempt.status == "running":
                attempt.status = "abandoned"
                attempt.finished_at = utcnow()
            return False
        previous = manga.status
        if previous == Status.CANCEL_REQUESTED.value and event != "cancelled":
            event = "cancelled"
            status = None
        if status is not None:
            target = Status(status)
            if (
                target != Status(previous)
                and not any(
                    destination == target
                    for destination in (
                        Status(value) for value in TRANSITIONS.get(Status(previous), {}).values()
                    )
                )
                and (target != Status.MANUAL_REVIEW or error_code is None)
            ):
                raise ValueError(f"Invalid transition {previous} -> {target.value}")
        elif event is not None:
            target = transition_target(previous, event)
        else:
            target = Status(previous)
        if event is not None and not can_transition(previous, event):
            raise ValueError(f"Invalid transition {previous} + {event}")
        now = utcnow()
        manga.status = target.value
        manga.status_updated_at = now
        manga.updated_at = now
        manga.row_version += 1
        manga.active_attempt_id = None
        manga.lease_token = None
        manga.lease_owner = None
        manga.lease_until = None
        manga.attempt_count = 0 if error_code is None else manga.attempt_count
        if event not in {"retry", "details_retry", "cleanup_retry"}:
            manga.next_retry_at = None
        manga.last_error_operation = claim.operation if error_code else None
        manga.last_error_code = error_code
        manga.last_error_detail = error_detail
        manga.last_error_at = now if error_code else None
        attempt.status = "failed" if error_code else "succeeded"
        attempt.finished_at = now
        attempt.resulting_status = target.value
        attempt.error_code = error_code
        self._event(
            manga.manga_id,
            "status_changed" if error_code is None else "error",
            actor=owner,
            attempt_id=claim.attempt_id,
            operation=claim.operation,
            from_status=previous,
            to_status=target.value,
            error_code=error_code,
            detail=detail or {},
        )
        return True

    def defer(self, claim: ClaimedAttempt, *, owner: str, retry_at: datetime) -> bool:
        """Finish a normal asynchronous poll without marking it as an error."""

        if not self.finish(claim, owner=owner):
            return False
        manga = self.session.get(MangaRecord, claim.manga_id)
        if manga is None:
            return False
        manga.next_retry_at = retry_at
        return True

    def mark_error_retry(
        self, claim: ClaimedAttempt, *, owner: str, error_code: str, detail: str, retry_at: datetime
    ) -> bool:
        manga = self.fenced(claim, owner=owner)
        if manga is None:
            return False
        manga.next_retry_at = retry_at
        return self.finish(
            claim, owner=owner, event="retry", error_code=error_code, error_detail=detail
        )

    def _event(
        self,
        manga_id: str | None,
        event_type: str,
        *,
        actor: str,
        attempt_id: int | None = None,
        operation: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        error_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            EventLog(
                manga_id=manga_id,
                attempt_id=attempt_id,
                run_id=self.run_id,
                component=operation or "repository",
                event_type=event_type,
                operation=operation,
                from_status=from_status,
                to_status=to_status,
                error_code=error_code,
                detail=detail or {},
                actor=actor,
            )
        )

    def set_component(
        self, component: str, state: str, *, actor: str, reason: str | None = None
    ) -> SystemControl:
        control = self.session.get(SystemControl, component)
        if control is None:
            control = SystemControl(
                component=component, state=state, updated_by=actor, reason=reason
            )
            self.session.add(control)
        else:
            control.state, control.reason, control.updated_by = state, reason, actor
            control.row_version += 1
        self.session.flush()
        self._event(
            None,
            "system_control",
            actor=actor,
            detail={"component": component, "state": state, "reason": reason},
        )
        return control
