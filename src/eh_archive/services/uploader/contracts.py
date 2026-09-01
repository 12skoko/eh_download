from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ...domain.models import MangaInfo


def _noop_progress(_transferred: int, _total: int) -> None:
    return None


def _noop_checkpoint() -> None:
    return None


def _noop_phase(_phase: str, _detail: Mapping[str, Any]) -> None:
    return None


def _noop_archive_identified(_archive_id: str) -> None:
    return None


@dataclass(frozen=True)
class UploadRequest:
    """Backend-neutral input for one already-validated upload attempt."""

    path: Path
    filename: str
    size: int
    sha1: str
    info: MangaInfo
    attempt_id: int
    timeout: float | tuple[float, float] | None = None
    expected_archive_id: str | None = None
    progress: Callable[[int, int], None] = field(default=_noop_progress, repr=False)
    checkpoint: Callable[[], None] = field(default=_noop_checkpoint, repr=False)
    phase: Callable[[str, Mapping[str, Any]], None] = field(default=_noop_phase, repr=False)
    archive_identified: Callable[[str], None] = field(
        default=_noop_archive_identified, repr=False
    )


@dataclass(frozen=True)
class UploadOutcome:
    kind: str
    archive_id: str | None = None
    status_code: int | None = None
    response: str = ""
    error_code: str | None = None


class UploadBackend(Protocol):
    def upload(self, request: UploadRequest) -> UploadOutcome: ...

