"""Daily counts of new archives and first completions by calendar date in the configured timezone."""

import statistics
from datetime import UTC, datetime, time, timedelta
from threading import Lock
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased

from ..db.models import EventLog, MangaRecord


SUMMARY_DAYS = 14
DEFAULT_TIMEZONE = "Asia/Shanghai"
SERIES = ("added", "completed")
# A day is clipped when it exceeds both 4x the series' 75th percentile and that
# percentile plus 1000, e.g. a one-off import far above the usual daily volume.
CLIP_RATIO = 4
CLIP_MARGIN = 1000
# With clipped bars present, the tallest ordinary bar uses this share of the plot.
CLIPPED_PEAK_SHARE = 0.78


def _completion(event):
    return (
        event.manga_id.is_not(None),
        event.to_status == "completed",
        event.from_status.is_distinct_from("completed"),
    )


def daily_summary(session, now, zone=ZoneInfo(DEFAULT_TIMEZONE)):
    today = now.astimezone(zone).date()
    dates = [today - timedelta(days=i) for i in reversed(range(SUMMARY_DAYS))]
    starts = [datetime.combine(day, time.min, zone).astimezone(UTC) for day in dates]
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

    series = {key: _series(days, key) for key in SERIES}
    # The scale follows the ordinary days only; clipped bars are drawn full
    # height and the ordinary peak is lowered so clipped bars stay the tallest.
    peak = max(day[key] for key in SERIES for day in days if not day[key + "_clipped"])
    clipped = any(item["clipped"] for item in series.values())
    ceiling = max(1, peak) / (CLIPPED_PEAK_SHARE if clipped else 1)
    for day in days:
        for key in SERIES:
            day[key + "_height"] = 100 if day[key + "_clipped"] else 100 * day[key] / ceiling
    return dict(days=days, series=series, as_of=now.astimezone(zone))


def _series(days, key):
    """Flag outlier days for one series and summarise the remaining days."""
    values = [day[key] for day in days]
    reference = statistics.quantiles(values, n=4, method="inclusive")[2]
    for day in days:
        value = day[key]
        day[key + "_clipped"] = value > CLIP_RATIO * reference and value - reference > CLIP_MARGIN
    ordinary = [day[key] for day in days if not day[key + "_clipped"]]
    return dict(
        total=sum(values),
        average=sum(ordinary) / len(ordinary),
        clipped=[day["date"] for day in days if day[key + "_clipped"]],
    )


class DailySummaryCache:
    def __init__(self, timezone: str = DEFAULT_TIMEZONE):
        self._zone = ZoneInfo(timezone)
        self._lock = Lock()
        self._summary = None

    def get(self, session):
        with self._lock:
            now = datetime.now(UTC)
            if self._summary is not None:
                previous = self._summary["as_of"]
                if (
                    previous.date() == now.astimezone(self._zone).date()
                    and timedelta(0) <= now - previous < timedelta(minutes=5)
                ):
                    return self._summary
            self._summary = daily_summary(session, now, self._zone)
            return self._summary
