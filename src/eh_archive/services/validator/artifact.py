from __future__ import annotations

import hashlib
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class ValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ArtifactFingerprint:
    path: Path
    kind: str
    size: int
    sha1: str | None
    checked_at: datetime


def _sha1(path: Path) -> tuple[str, int]:
    sha1 = hashlib.sha1()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            sha1.update(chunk)
    return sha1.hexdigest(), size


def _validate_zip(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise ValidationError("invalid_archive", "file is not a ZIP archive")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos:
                raise ValidationError("empty_archive", "ZIP archive contains no entries")
            files = [item for item in infos if not item.is_dir()]
            if not files:
                raise ValidationError("empty_archive", "ZIP archive contains no files")
            for item in infos:
                # Reject absolute paths and traversal before extraction or upload.
                name = item.filename.replace("\\", "/")
                if name.startswith("/") or ":" in name.split("/", 1)[0] or ".." in Path(name).parts:
                    raise ValidationError(
                        "unsafe_archive_path", f"unsafe ZIP member: {item.filename}"
                    )
            if archive.testzip() is not None:
                raise ValidationError("crc_error", "ZIP CRC check failed")
    except zipfile.BadZipFile as exc:
        raise ValidationError("truncated_archive", str(exc)) from exc


def _validate_directory(path: Path) -> int:
    file_count = 0
    size = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValidationError("symlink_member", f"directory contains symlink: {item}")
        if item.is_file():
            file_count += 1
            size += item.stat().st_size
    if not file_count:
        raise ValidationError("empty_directory", "directory contains no files")
    return size


def validate_artifact(
    path: str | Path,
    *,
    expected_kind: str | None = None,
    max_size: int | None = None,
    calculate_sha1: bool = True,
) -> ArtifactFingerprint:
    path = Path(path)
    if not path.exists():
        raise ValidationError("missing_artifact", str(path))
    if path.is_dir():
        if expected_kind == "file":
            raise ValidationError("unexpected_directory", "directory cannot be uploaded as a file")
        size = _validate_directory(path)
        if max_size is not None and size > max_size:
            raise ValidationError("artifact_too_large", f"{size} > {max_size}")
        kind = "directory"
        sha1 = None
    else:
        size = path.stat().st_size
        if size <= 0:
            raise ValidationError("empty_artifact", "artifact is empty")
        if max_size is not None and size > max_size:
            raise ValidationError("artifact_too_large", f"{size} > {max_size}")
        with path.open("rb") as handle:
            prefix = handle.read(512).lstrip().lower()
        if prefix.startswith((b"<!doctype html", b"<html", b"{", b"[")):
            raise ValidationError("error_page", "artifact looks like an HTML/JSON response")
        observed_size = size
        _validate_zip(path)
        if calculate_sha1:
            sha1, size = _sha1(path)
        else:
            sha1 = None
        if path.stat().st_size != observed_size or size != observed_size:
            raise ValidationError("artifact_changing", "artifact changed during validation")
        kind = "zip"
    if expected_kind and expected_kind != kind:
        raise ValidationError("kind_mismatch", f"expected {expected_kind}, got {kind}")
    return ArtifactFingerprint(path, kind, size, sha1, datetime.now(UTC))


def quarantine_artifact(path: str | Path, destination: str | Path) -> Path:
    source, destination = Path(path), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return destination
    shutil.move(str(source), str(destination))
    return destination
