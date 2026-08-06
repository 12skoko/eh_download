from __future__ import annotations

import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..validator.artifact import ArtifactFingerprint, validate_artifact


@dataclass(frozen=True)
class PrepareResult:
    path: Path
    fingerprint: ArtifactFingerprint


def prepare_directory(
    source: str | Path,
    temporary: str | Path,
    final: str | Path,
    *,
    before_promote: Callable[[], None] | None = None,
) -> PrepareResult:
    source, temporary, final = Path(source), Path(temporary), Path(final)
    if not source.is_dir():
        raise ValueError(f"prepare source is not a directory: {source}")
    if final.exists():
        raise FileExistsError(f"artifact generation already exists: {final}")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(
        temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as archive:
        for item in sorted(source.rglob("*")):
            if item.is_symlink():
                raise ValueError(f"source contains symlink: {item}")
            if item.is_file():
                archive.write(item, item.relative_to(source).as_posix())
    # Validation happens before the final name becomes visible to upload tasks.
    fingerprint = validate_artifact(temporary, expected_kind="zip")
    if before_promote is not None:
        before_promote()
    os.replace(temporary, final)
    return PrepareResult(final, replace(fingerprint, path=final))
