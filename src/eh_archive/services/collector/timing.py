from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ...domain.models import Manga
from ...domain.states import Status


def observation_deadline(posted_at: datetime, observation_days: int) -> datetime:
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    return posted_at + timedelta(days=observation_days)


def collection_status(
    manga: Manga,
    observation_days: int,
    *,
    now: datetime | None = None,
) -> tuple[Status, datetime | None, str]:
    """Return the collection-owned time state for one parsed gallery."""

    if manga.posted_at is None:
        raise ValueError("posted_at is required before collection state can be calculated")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    deadline = observation_deadline(manga.posted_at, observation_days)
    if current < deadline:
        return Status.DEFERRED, deadline, "observation_period"
    return Status.DISCOVERED, None, "awaiting_screen"
