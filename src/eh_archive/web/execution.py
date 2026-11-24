"""Request-scoped database limits; workers retain their existing transaction policy."""

from contextvars import ContextVar
from time import monotonic

from fastapi.responses import JSONResponse
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError

from ..logging import get_logger


_web_write = ContextVar("web_write", default=False)
LOCK_TIMEOUT_MS = 3000
_logger = get_logger("web")


def _limit_lock_wait(session, transaction, connection):
    # after_begin also covers a new transaction after an explicit commit/rollback.
    # SET LOCAL expires at transaction end, including on pooled connections.
    if _web_write.get() and connection.dialect.name == "postgresql":
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")


def install_execution_policy(app, database, ctx):
    if not event.contains(database.session_factory, "after_begin", _limit_lock_wait):
        event.listen(database.session_factory, "after_begin", _limit_lock_wait)

    @app.middleware("http")
    async def request_scope(request, call_next):
        token = _web_write.set(request.method not in {"GET", "HEAD", "OPTIONS"})
        started = monotonic()
        try:
            return await call_next(request)
        finally:
            elapsed = monotonic() - started
            _web_write.reset(token)
            if elapsed >= 1:
                _logger.warning(
                    "Slow Web request: method=%s path=%s elapsed=%.3fs",
                    request.method, request.url.path, elapsed,
                )

    @app.exception_handler(DBAPIError)
    def database_error(request, exc):
        code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if code != "55P03":
            raise exc
        # Database.session has already rolled back before the exception arrives here.
        message = "数据库记录正在被其他任务占用，锁等待已超时。请刷新页面确认状态后重试。"
        _logger.warning("Web database lock timeout: method=%s path=%s", request.method, request.url.path)
        if request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": message}, status_code=409)
        return ctx.error(request, message, 409)
