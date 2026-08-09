from __future__ import annotations

import logging
import os
import re
import traceback
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Self

from .structured import _load_timezone

log = logging.getLogger(__name__)


def clean_report_value(value: Any) -> str:
    """Keep one logical report value on one physical line."""

    if value is None:
        return ""
    return " ".join(str(value).replace("|", "/").splitlines()).strip()


def format_report_size(size: int | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def format_report_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:.2f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes}m {remainder:.2f}s"


def format_report_datetime(value: datetime | None, timezone: str) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_load_timezone(timezone)).isoformat(timespec="seconds")


class RunReport:
    """A concise, human-readable report for one child-process invocation."""

    def __init__(
        self,
        log_dir: str | Path,
        module: str,
        *,
        timezone: str = "UTC",
        run_id: str | None = None,
        pid: int | None = None,
    ) -> None:
        self.module = module
        self.run_id = run_id or str(uuid.uuid4())
        self.pid = pid or os.getpid()
        self.timezone = timezone
        self.started = monotonic()
        self.path: Path | None = None
        self._stream = None
        self._closed = False
        self._write_failed = False

        safe_module = re.sub(r"[^A-Za-z0-9_-]+", "_", module).strip("_") or "unknown"
        started_at = datetime.now(_load_timezone(timezone)).strftime("%Y%m%d_%H%M%S")
        path = Path(log_dir) / "detail" / safe_module / f"{started_at}_{self.run_id}.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("x", encoding="utf-8", buffering=1)
        except OSError:
            log.warning("failed to create detailed run report: %s", path, exc_info=True)
            return
        self.path = path
        self.fields({"module": module, "run_id": self.run_id, "pid": self.pid})

    def fields(self, values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            self.write(f"{key}: {clean_report_value(value)}")

    def section(self, title: str) -> None:
        self.write("")
        self.write(f"=== {clean_report_value(title)} ===")
        self.write("")

    def lines(self, values: Iterable[str]) -> None:
        for value in values:
            self.write(value)

    def write(self, value: str) -> None:
        if self._stream is None or self._closed or self._write_failed:
            return
        try:
            self._stream.write(f"{value}\n")
            self._stream.flush()
        except OSError:
            self._write_failed = True
            log.warning("failed to write detailed run report: %s", self.path, exc_info=True)

    def finish(self, values: Mapping[str, Any]) -> None:
        if self._closed:
            return
        self.section("result")
        self.fields(values)
        if "duration" not in values:
            self.write(f"duration: {format_report_duration(monotonic() - self.started)}")
        self.close()

    def fatal(
        self,
        exc: BaseException,
        *,
        current_manga: str | None = None,
        attempt_id: int | None = None,
        context: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        if self._closed:
            return
        self.section("fatal error")
        if current_manga:
            self.write(f"current_manga: {clean_report_value(current_manga)}")
        if attempt_id is not None:
            self.write(f"attempt_id: {attempt_id}")
        if context:
            self.fields(context)
        self.write(f"error_type: {type(exc).__name__}")
        self.write(f"error: {clean_report_value(exc)}")
        self.write("traceback:")
        for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for physical_line in line.rstrip().splitlines():
                self.write(f"  {physical_line}")
        values = dict(result or {})
        values["status"] = "failed"
        self.finish(values)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stream is None:
            return
        try:
            self._stream.close()
        except OSError:
            log.warning("failed to close detailed run report: %s", self.path, exc_info=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if exc is not None:
            self.fatal(exc)
        else:
            self.close()
        return False
