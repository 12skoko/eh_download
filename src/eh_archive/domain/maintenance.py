from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .errors import ArchiveError, ErrorClass


class MaintenancePaused(ArchiveError):
    def __init__(self) -> None:
        super().__init__("maintenance_paused", "每日维护窗口，任务已暂停", ErrorClass.TEMPORARY)


def in_maintenance_window(config: Any, timezone: str | Any, now: datetime | None = None) -> bool:
    """One daily window for both scheduling and worker checkpoints."""
    start = config.maintenance_start
    end = config.maintenance_end
    if start is None or end is None:
        return False
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    current = (now.astimezone(zone) if now is not None else datetime.now(zone)).time()
    current = current.replace(tzinfo=None)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def check_maintenance(config: Any, timezone: str) -> None:
    if in_maintenance_window(config, timezone):
        raise MaintenancePaused()
