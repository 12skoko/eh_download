from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAIN_LOG_ENV = "EHARCHIVE_MAIN_LOG"
SUPERVISOR_RUN_ID_ENV = "EHARCHIVE_SUPERVISOR_RUN_ID"
SMB_LOGGER_NAMES = ("smbprotocol", "smbprotocol.open", "smbclient")


def _public_logger_name(name: str) -> str:
    if any(name == prefix or name.startswith(f"{prefix}.") for prefix in SMB_LOGGER_NAMES):
        return "upload"
    return name


class JsonFormatter(logging.Formatter):
    def __init__(self, timezone: str = "UTC") -> None:
        super().__init__()
        self.timezone = _load_timezone(timezone)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.now(self.timezone).isoformat(),
            "level": record.levelname,
            "logger": _public_logger_name(record.name),
            "message": record.getMessage(),
        }
        if hasattr(record, "event"):
            payload["event"] = record.event
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _load_timezone(timezone: str):
    if timezone.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone}") from exc


def session_log_path(
    log_dir: str | Path,
    component: str,
    *,
    timezone: str = "UTC",
    run_id: str | None = None,
) -> Path:
    safe_component = re.sub(r"[^A-Za-z0-9_-]+", "_", component).strip("_") or "application"
    started_at = datetime.now(_load_timezone(timezone)).strftime("%Y%m%d_%H%M%S")
    identifier = run_id or str(uuid.uuid4())
    return Path(log_dir) / safe_component / f"{started_at}_{identifier}.log"


def configure_logging(
    level: str = "INFO",
    log_dir: str | Path | None = None,
    *,
    timezone: str = "UTC",
    component: str = "application",
    run_id: str | None = None,
    log_file: str | Path | None = None,
) -> Path | None:
    formatter = JsonFormatter(timezone)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in root.handlers:
        existing.close()
    root.handlers.clear()
    root.addHandler(handler)
    path: Path | None = None
    if log_dir is not None:
        inherited = os.environ.get(MAIN_LOG_ENV)
        path = (
            Path(log_file or inherited).resolve()
            if log_file or inherited
            else session_log_path(log_dir, component, timezone=timezone, run_id=run_id).resolve()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    root.setLevel(level.upper())
    # smbprotocol emits one INFO record per SMB request/response. Large file
    # transfers would otherwise flood both stdout and the shared session log.
    for logger_name in SMB_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    return path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
