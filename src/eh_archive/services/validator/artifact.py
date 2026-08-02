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
    sha256: str
    sha1: str
    checked_at: datetime


def _hashes(path: Path) -> tuple[str, str, int]:
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            sha256.update(chunk)
            sha1.update(chunk)
    return sha256.hexdigest(), sha1.hexdigest(), size


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


def _validate_directory(path: Path) -> None:
    files = []
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValidationError("symlink_member", f"directory contains symlink: {item}")
        if item.is_file():
            files.append(item)
    if not files:
        raise ValidationError("empty_directory", "directory contains no files")


def validate_artifact(
    path: str | Path, *, expected_kind: str | None = None, max_size: int | None = None
) -> ArtifactFingerprint:
    path = Path(path)
    if not path.exists():
        raise ValidationError("missing_artifact", str(path))
    if path.is_dir():
        if expected_kind == "file":
            raise ValidationError("unexpected_directory", "directory cannot be uploaded as a file")
        _validate_directory(path)
        digest256 = hashlib.sha256()
        digest1 = hashlib.sha1()
        size = 0
        for item in sorted(x for x in path.rglob("*") if x.is_file()):
            h256, h1, item_size = _hashes(item)
            relative_name = item.relative_to(path).as_posix()
            digest256.update(relative_name.encode())
            digest256.update(h256.encode())
            digest1.update(relative_name.encode())
            digest1.update(h1.encode())
            size += item_size
        if max_size is not None and size > max_size:
            raise ValidationError("artifact_too_large", f"{size} > {max_size}")
        kind = "directory"
        sha256, sha1 = digest256.hexdigest(), digest1.hexdigest()
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
        sha256, sha1, size = _hashes(path)
        if path.stat().st_size != observed_size or size != observed_size:
            raise ValidationError("artifact_changing", "artifact changed during validation")
        kind = "zip"
    if expected_kind and expected_kind != kind:
        raise ValidationError("kind_mismatch", f"expected {expected_kind}, got {kind}")
    return ArtifactFingerprint(path, kind, size, sha256, sha1, datetime.now(UTC))


def quarantine_artifact(path: str | Path, destination: str | Path) -> Path:
    source, destination = Path(path), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return destination
    shutil.move(str(source), str(destination))
    return destination
