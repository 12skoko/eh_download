from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AppConfig, SecretsConfig, SupervisorConfig
from ..db import Database, SystemHealth
from ..db.repository import utcnow
from ..domain.errors import ArchiveError, ErrorClass, classify_exception
from ..services.uploader.lanraragi import LANraragiClient


@dataclass(frozen=True)
class HealthResult:
    component: str
    status: str
    latency_ms: int
    error_code: str | None
    message: str
    detail: dict[str, Any]


def refresh_health_snapshots(
    database: Database,
    app: AppConfig,
    supervisor: SupervisorConfig,
    secrets: SecretsConfig,
) -> list[HealthResult]:
    probes: list[tuple[str, Callable[[], tuple[str, str, dict[str, Any]]]]] = [
        ("qbittorrent", lambda: _check_qbittorrent(app, supervisor, secrets)),
        ("lanraragi", lambda: _check_lanraragi(app, supervisor, secrets)),
    ]
    probes.extend(
        (f"storage:{location}", lambda root=root: _check_storage(root))
        for location, root in app.roots.items()
    )
    results = [_run_probe(component, probe) for component, probe in probes]
    checked_at = utcnow()
    with database.session() as session:
        for result in results:
            row = session.get(SystemHealth, result.component)
            if row is None:
                row = SystemHealth(component=result.component)
                session.add(row)
            row.status = result.status
            row.checked_at = checked_at
            row.latency_ms = result.latency_ms
            row.error_code = result.error_code
            row.message = result.message
            row.detail = result.detail
            row.updated_at = checked_at
    return results


def _run_probe(
    component: str, probe: Callable[[], tuple[str, str, dict[str, Any]]]
) -> HealthResult:
    started = time.perf_counter()
    try:
        status, message, detail = probe()
        return HealthResult(
            component,
            status,
            round((time.perf_counter() - started) * 1000),
            None,
            message,
            detail,
        )
    except Exception as exc:  # noqa: BLE001 - every probe must become a snapshot
        info = classify_exception(exc)
        return HealthResult(
            component,
            "unavailable",
            round((time.perf_counter() - started) * 1000),
            info.code,
            _health_error_message(component, info.code),
            {},
        )


def _check_qbittorrent(
    app: AppConfig, supervisor: SupervisorConfig, secrets: SecretsConfig
) -> tuple[str, str, dict[str, Any]]:
    from ..integrations.qbittorrent import QBittorrentClient

    options = dict(secrets.qbittorrent)
    options.setdefault("host", app.qbittorrent_url)
    options.setdefault("REQUESTS_ARGS", {"timeout": min(supervisor.request_timeout_seconds, 5)})
    version = QBittorrentClient(**options).version()
    return "healthy", "连接正常", {"version": version}


def _check_lanraragi(
    app: AppConfig, supervisor: SupervisorConfig, secrets: SecretsConfig
) -> tuple[str, str, dict[str, Any]]:
    status, payload = LANraragiClient(
        app.lanraragi_url,
        headers=secrets.lanraragi,
        timeout=min(supervisor.request_timeout_seconds, 5),
    ).info()
    if status in {401, 403}:
        raise ArchiveError(
            "lanraragi_auth_failed", "LANraragi rejected authentication", ErrorClass.SYSTEM
        )
    if status != 200 or payload is None:
        raise ArchiveError(
            "lanraragi_unavailable",
            f"LANraragi health endpoint returned HTTP {status}",
            ErrorClass.SYSTEM,
        )
    version = payload.get("version") or payload.get("server_version")
    detail = {"version": str(version)} if version else {}
    return "healthy", "连接正常", detail


def _check_storage(root: Path) -> tuple[str, str, dict[str, Any]]:
    if not root.is_dir():
        raise ArchiveError("storage_missing", "storage directory does not exist", ErrorClass.SYSTEM)
    usage = shutil.disk_usage(root)
    with tempfile.NamedTemporaryFile(prefix=".eharchive-health-", dir=root, delete=True) as handle:
        handle.write(b"ok")
        handle.flush()
        os.fsync(handle.fileno())
    free_ratio = usage.free / usage.total if usage.total else 0
    status = "degraded" if usage.free < 5 * 1024**3 or free_ratio < 0.05 else "healthy"
    message = "空间偏低" if status == "degraded" else "读写正常"
    return status, message, {"free_bytes": usage.free, "total_bytes": usage.total}


def _health_error_message(component: str, error_code: str) -> str:
    if component == "qbittorrent":
        return "qBittorrent 无法连接或认证失败"
    if component == "lanraragi":
        return "LANraragi 无法连接或认证失败"
    if component.startswith("storage:"):
        return "存储目录不可用"
    return f"健康检查失败：{error_code}"
