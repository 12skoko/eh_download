"""Daily counts of new archives and first completions by Beijing calendar date."""

from datetime import UTC, datetime, time, timedelta, timezone
from threading import Lock

from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased

from ..db.models import EventLog, MangaRecord


BEIJING = timezone(timedelta(hours=8))
SUMMARY_DAYS = 14


def _completion(event):
    return (
        event.manga_id.is_not(None),
        event.to_status == "completed",
        event.from_status.is_distinct_from("completed"),
    )


def daily_summary(session, now):
    today = now.astimezone(BEIJING).date()
    dates = [today - timedelta(days=i) for i in reversed(range(SUMMARY_DAYS))]
    starts = [datetime.combine(day, time.min, BEIJING).astimezone(UTC) for day in dates]
    days = [dict(date=day, added=0, completed=0) for day in dates]

    # Restrict the candidate set by time first. For each candidate the existing
    # (manga_id, created_at) index also covers the lookup before the window.
    earlier = aliased(EventLog)
    earlier_completion = select(earlier.created_at).where(
        earlier.manga_id == EventLog.manga_id,
        earlier.created_at < starts[0],
        *_completion(earlier),
    ).limit(1).scalar_subquery()
    first = select(func.min(EventLog.created_at).label("time")).where(
        EventLog.created_at.between(starts[0], now),
        *_completion(EventLog),
    ).group_by(EventLog.manga_id).having(earlier_completion.is_(None)).subquery()

    for key, stamp in (("added", MangaRecord.created_at), ("completed", first.c.time)):
        # UTC boundaries work for PostgreSQL and the SQLite demo, independently
        # of the database session's local timezone.
        day_index = case(*[(stamp >= starts[i], i) for i in range(len(starts) - 1, 0, -1)], else_=0)
        query = select(day_index.label("day_index"), func.count()).group_by("day_index")
        if key == "added":
            query = query.where(stamp.between(starts[0], now))
        for index, count in session.execute(query):
            days[index][key] = count

    peak = max(max(day["added"], day["completed"]) for day in days)
    # Integer ticks spaced by 1, 2 or 5 times a power of ten.
    target = max(1, (peak + 6) // 7)
    magnitude = 10 ** (len(str(target)) - 1)
    step = next(magnitude * n for n in (1, 2, 5, 10) if magnitude * n >= target)
    ceiling = max(1, (peak + step - 1) // step) * step
    return dict(days=days, ceiling=ceiling, ticks=range(ceiling, -1, -step),
                as_of=now.astimezone(BEIJING))


class DailySummaryCache:
    def __init__(self):
        self._lock = Lock()
        self._summary = None

    def get(self, session):
        with self._lock:
            now = datetime.now(UTC)
            if self._summary is not None:
                previous = self._summary["as_of"]
                if (
                    previous.date() == now.astimezone(BEIJING).date()
                    and timedelta(0) <= now - previous < timedelta(minutes=5)
                ):
                    return self._summary
            self._summary = daily_summary(session, now)
            return self._summary
