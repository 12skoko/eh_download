from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from errno import EACCES, EDQUOT, ENOSPC, EROFS
from typing import Any


class ErrorClass(StrEnum):
    TEMPORARY = "temporary"
    ITEM = "item"
    SYSTEM = "system"


@dataclass
class ErrorInfo:
    code: str
    message: str
    category: ErrorClass
    retryable: bool = False
    status_code: int | None = None
    detail: dict[str, Any] | None = None


class ArchiveError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        category: ErrorClass = ErrorClass.ITEM,
        *,
        retryable: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.info = ErrorInfo(code, message, category, retryable, detail=detail)


def classify_exception(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, ArchiveError):
        return exc.info
    try:
        from sqlalchemy.exc import DBAPIError, DisconnectionError, OperationalError

        if isinstance(exc, (DBAPIError, DisconnectionError, OperationalError)):
            return ErrorInfo("database_unavailable", str(exc), ErrorClass.SYSTEM)
    except ImportError:
        pass
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in {408, 425, 429, 500, 502, 503, 504}:
        return ErrorInfo(
            "http_temporary",
            str(exc),
            ErrorClass.TEMPORARY,
            retryable=True,
            status_code=int(status),
        )
    if status in {401, 403}:
        return ErrorInfo("http_forbidden", str(exc), ErrorClass.SYSTEM, status_code=int(status))
    if status in {404, 410}:
        return ErrorInfo("http_unavailable", str(exc), ErrorClass.ITEM, status_code=int(status))
    name = type(exc).__name__.lower()
    if "proxy" in name:
        return ErrorInfo("proxy_unavailable", str(exc), ErrorClass.SYSTEM)
    if any(term in name for term in ("timeout", "connection", "reset", "temporary")):
        return ErrorInfo("network_temporary", str(exc), ErrorClass.TEMPORARY, retryable=True)
    if isinstance(exc, RuntimeError):
        return ErrorInfo("system_runtime_error", str(exc), ErrorClass.SYSTEM)
    if isinstance(exc, OSError):
        code = {
            ENOSPC: "disk_full",
            EDQUOT: "disk_quota_exceeded",
            EROFS: "filesystem_read_only",
            EACCES: "filesystem_permission_denied",
        }.get(exc.errno, "filesystem_error")
        return ErrorInfo(code, str(exc), ErrorClass.SYSTEM)
    return ErrorInfo("unexpected_error", str(exc), ErrorClass.ITEM)
