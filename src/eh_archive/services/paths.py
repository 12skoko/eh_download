from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..config.loader import AppConfig


class UnsafePathError(ValueError):
    pass


def safe_manga_id(manga_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", manga_id.replace("/", "_"))
    value = value.strip("._-") or "manga"
    if len(value) > 120:
        value = value[:108] + "_" + hashlib.sha1(manga_id.encode("utf-8")).hexdigest()[:11]
    return value


def safe_filename(name: str, *, max_length: int = 180) -> str:
    if not name or "\x00" in name:
        raise UnsafePathError("empty or NUL filename")
    if Path(name).name != name or "/" in name or "\\" in name:
        raise UnsafePathError("filename must not contain path separators")
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    value = value.strip(" .")
    if value in {"", ".", ".."}:
        raise UnsafePathError("invalid filename")
    reserved = value.split(".", 1)[0].upper()
    if reserved in {"CON", "PRN", "AUX", "NUL"} or (
        len(reserved) == 4 and reserved[:3] in {"COM", "LPT"} and reserved[3].isdigit()
    ):
        raise UnsafePathError("reserved Windows filename")
    if len(value) > max_length or len(value.encode("utf-8")) > max_length:
        suffix = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
        stem, dot, extension = value.rpartition(".")
        if dot and len(extension) < 20:
            budget = max_length - len(suffix.encode("utf-8")) - len(extension.encode("utf-8")) - 3
            trimmed = stem[:budget]
            while trimmed and len(trimmed.encode("utf-8")) > budget:
                trimmed = trimmed[:-1]
            value = trimmed + "_" + suffix + "." + extension
        else:
            budget = max_length - len(suffix) - 1
            trimmed = value[:budget]
            while trimmed and len(trimmed.encode("utf-8")) > budget:
                trimmed = trimmed[:-1]
            value = trimmed + "_" + suffix
    return value


@dataclass(frozen=True)
class ArtifactPaths:
    root: Path
    final: Path
    temporary: Path
    quarantine: Path


class ArtifactPathService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _root(self, location: str) -> Path:
        root = self.config.root(location).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    @staticmethod
    def _inside(root: Path, child: Path) -> bool:
        try:
            child.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def resolve(self, location: str, filename: str) -> Path:
        root = self._root(location)
        safe = safe_filename(filename)
        result = root / safe
        if not self._inside(root, result):
            raise UnsafePathError("artifact path escapes configured root")
        return result

    def names(
        self, manga_id: str, generation: int, attempt_id: int | None = None, extension: str = ".zip"
    ) -> tuple[str, str]:
        ident = safe_manga_id(manga_id)
        ext = extension if extension.startswith(".") else f".{extension}"
        final = f"{ident}.g{generation}{ext}"
        temporary = f"{ident}.g{generation}.a{attempt_id or 'pending'}.tmp"
        return final, temporary

    def for_attempt(
        self,
        *,
        manga_id: str,
        generation: int,
        attempt_id: int,
        location: str,
        extension: str = ".zip",
    ) -> ArtifactPaths:
        root = self._root(location)
        final_name, temporary_name = self.names(manga_id, generation, attempt_id, extension)
        final = self.resolve(location, final_name)
        temporary = self.resolve(location, temporary_name)
        quarantine_root = self._root("quarantine")
        quarantine = (
            quarantine_root / f"{safe_manga_id(manga_id)}.g{generation}.a{attempt_id}.quarantine"
        )
        if not self._inside(quarantine_root, quarantine):
            raise UnsafePathError("quarantine path escapes configured root")
        return ArtifactPaths(root, final, temporary, quarantine)

    def validate_registered(self, location: str, filename: str) -> Path:
        path = self.resolve(location, filename)
        # Refuse symlink/reparse traversal for current artifacts. Existing files
        # are checked before every upload or cleanup operation.
        if path.is_symlink():
            raise UnsafePathError("artifact path is a symlink")
        root = self._root(location)
        current = root
        for part in path.relative_to(root).parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise UnsafePathError("artifact path traverses a symlink")
        return path

    def torrent_registered(self, manga_id: str, filename: str) -> Path:
        """Resolve qBittorrent's per-gallery directory without storing it in DB."""
        root = self._root("torrent_download")
        folder = safe_filename(manga_id.split("/", 1)[0])
        name = safe_filename(filename)
        folder_path = root / folder
        if folder_path.is_symlink():
            raise UnsafePathError("torrent artifact path traverses a symlink")
        result = folder_path / name
        if not self._inside(root, result):
            raise UnsafePathError("torrent artifact escapes configured root")
        if result.is_symlink():
            raise UnsafePathError("torrent artifact path is a symlink")
        return result
