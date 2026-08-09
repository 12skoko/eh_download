from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import EventLog, MangaRecord
from .lanraragi import LANraragiClient


@dataclass(frozen=True)
class ThumbnailBatchResult:
    attempted: int = 0
    accepted: int = 0
    failed: int = 0
    skipped: int = 0
    items: tuple[ThumbnailItem, ...] = ()


@dataclass(frozen=True)
class ThumbnailItem:
    manga_id: str
    archive_id: str
    outcome: str
    error_code: str | None = None
    detail: str | None = None


class ThumbnailBatch:
    """Regenerate thumbnails in an idempotent, independent batch."""

    STATUSES = ("uploaded", "completed")

    def __init__(
        self,
        session: Session,
        client: LANraragiClient,
        *,
        actor: str = "thumbnail",
        run_id: str | None = None,
    ):
        self.session = session
        self.client = client
        self.actor = actor
        self.run_id = run_id

    @classmethod
    def has_work(cls, session: Session, *, limit: int = 1) -> bool:
        if limit <= 0:
            return False
        rows = list(
            session.scalars(
                select(MangaRecord)
                .where(
                    MangaRecord.status.in_(cls.STATUSES),
                    MangaRecord.lrr_archive_id.is_not(None),
                )
                .order_by(MangaRecord.status_updated_at)
                .limit(max(1, limit * 5))
            )
        )
        return any(cls._needs_refresh(session, row) for row in rows)

    @staticmethod
    def _needs_refresh(session: Session, row: MangaRecord) -> bool:
        return (
            session.scalar(
                select(EventLog.id)
                .where(
                    EventLog.manga_id == row.manga_id,
                    EventLog.event_type == "thumbnail_regenerated",
                    EventLog.created_at >= row.status_updated_at,
                )
                .limit(1)
            )
            is None
        )

    def run(self, *, limit: int = 100) -> ThumbnailBatchResult:
        if limit <= 0:
            return ThumbnailBatchResult()
        rows = list(
            self.session.scalars(
                select(MangaRecord)
                .where(
                    MangaRecord.status.in_(self.STATUSES),
                    MangaRecord.lrr_archive_id.is_not(None),
                )
                .order_by(MangaRecord.status_updated_at)
                .limit(max(1, limit))
            )
        )
        attempted = accepted = failed = skipped = 0
        items: list[ThumbnailItem] = []
        for row in rows:
            if not self._needs_refresh(self.session, row):
                skipped += 1
                items.append(
                    ThumbnailItem(str(row.manga_id), str(row.lrr_archive_id or ""), "skipped")
                )
                continue
            archive_id = str(row.lrr_archive_id)
            if not re.fullmatch(r"[0-9a-fA-F]{40}", archive_id):
                self._event(
                    row,
                    "thumbnail_error",
                    error_code="invalid_archive_id",
                    detail={"archive_id": archive_id},
                )
                failed += 1
                items.append(
                    ThumbnailItem(str(row.manga_id), archive_id, "failed", "invalid_archive_id")
                )
                continue
            attempted += 1
            try:
                outcome = self.client.regenerate_thumbnails(archive_id)
            except Exception as exc:  # noqa: BLE001 - batch must continue per archive
                self._event(
                    row,
                    "thumbnail_error",
                    error_code=type(exc).__name__,
                    detail={"message": str(exc)[:1000]},
                )
                failed += 1
                items.append(
                    ThumbnailItem(
                        str(row.manga_id),
                        archive_id,
                        "failed",
                        type(exc).__name__,
                        str(exc)[:1000],
                    )
                )
                continue
            if outcome.kind == "accepted":
                self._event(
                    row,
                    "thumbnail_regenerated",
                    detail={"status_code": outcome.status_code},
                )
                accepted += 1
                items.append(ThumbnailItem(str(row.manga_id), archive_id, "regenerated"))
            else:
                self._event(
                    row,
                    "thumbnail_error",
                    error_code=f"lrr_{outcome.status_code or outcome.kind}",
                    detail={
                        "status_code": outcome.status_code,
                        "kind": outcome.kind,
                        "response": outcome.response[:1000],
                    },
                )
                failed += 1
                items.append(
                    ThumbnailItem(
                        str(row.manga_id),
                        archive_id,
                        "failed",
                        f"lrr_{outcome.status_code or outcome.kind}",
                        outcome.response[:1000],
                    )
                )
        return ThumbnailBatchResult(attempted, accepted, failed, skipped, tuple(items))

    def _event(
        self,
        row: MangaRecord,
        event_type: str,
        *,
        error_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            EventLog(
                manga_id=row.manga_id,
                run_id=self.run_id,
                component="thumbnail",
                event_type=event_type,
                operation="thumbnail",
                error_code=error_code,
                actor=self.actor,
                detail=detail or {},
            )
        )
