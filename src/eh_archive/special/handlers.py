from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..db import Database
from .registry import VIDEO_ARCHIVE_KIND
from .repository import ClaimedSpecialJob


class SpecialExecutor(Protocol):
    def run(self) -> None: ...


ExecutorFactory = Callable[[Database, str | Path, ClaimedSpecialJob], SpecialExecutor]


@dataclass(frozen=True)
class ModuleCapability:
    kind: str
    enabled: bool
    max_concurrency: int
    reason: str | None = None


def _video_archive_factory(
    database: Database,
    config_dir: str | Path,
    claim: ClaimedSpecialJob,
) -> SpecialExecutor:
    # Lazy import keeps the handler registry independent of the video module's
    # own operation-registry imports.
    from .video import VideoArchiveExecutor

    return VideoArchiveExecutor(database, config_dir=config_dir, claim=claim)


EXECUTOR_REGISTRY: dict[str, ExecutorFactory] = {
    VIDEO_ARCHIVE_KIND: _video_archive_factory,
}


def module_capability(kind: str, config_dir: str | Path) -> ModuleCapability:
    """Read module configuration without probing files, services or ffmpeg."""

    if kind != VIDEO_ARCHIVE_KIND or kind not in EXECUTOR_REGISTRY:
        return ModuleCapability(kind, False, 0, "特殊处理模块未安装")
    try:
        from ..config import load_video_archive_config

        config = load_video_archive_config(config_dir)
    except (OSError, TypeError, ValueError) as exc:
        return ModuleCapability(kind, False, 0, str(exc))
    if not config.enabled:
        return ModuleCapability(kind, False, 0, "video_archive.enabled=false")
    return ModuleCapability(kind, True, config.work.max_concurrency)


def enabled_module_capabilities(config_dir: str | Path) -> tuple[ModuleCapability, ...]:
    return tuple(
        capability
        for kind in EXECUTOR_REGISTRY
        if (capability := module_capability(kind, config_dir)).enabled
    )


def build_executor(
    kind: str,
    database: Database,
    *,
    config_dir: str | Path,
    claim: ClaimedSpecialJob,
) -> SpecialExecutor:
    try:
        factory = EXECUTOR_REGISTRY[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported special workflow handler: {kind}") from exc
    return factory(database, config_dir, claim)
