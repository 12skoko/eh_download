from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..config.loader import AppConfig


class UnsafePathError(ValueError):
    pass


def _normalise_external_path(value: str | Path) -> str:
    text = str(value).strip().replace("\\", "/")
    if text != "/" and not re.fullmatch(r"[A-Za-z]:/", text):
        text = text.rstrip("/")
    return text


def map_external_path(
    external_path: str | Path,
    external_root: str | Path,
    local_root: str | Path,
) -> Path:
    """Map a qBittorrent-visible path into the local artifact root.

    qBittorrent may run on another host, so its absolute path cannot be
    resolved with the local filesystem. Only the relative suffix below the
    configured external root is transferred to the local root.
    """

    external = _normalise_external_path(external_path)
    root = _normalise_external_path(external_root)
    if not root:
        raise UnsafePathError("external artifact root is empty")
    if external == root:
        relative = ""
    else:
        prefix = root if root.endswith("/") else root + "/"
        if not external.startswith(prefix):
            raise UnsafePathError(
                f"external path {external_path!r} is outside configured root {external_root!r}"
            )
        relative = external[len(prefix) :]
    parts = tuple(part for part in relative.split("/") if part)
    if any(part in {".", ".."} or "\x00" in part for part in parts):
        raise UnsafePathError("external artifact path contains an unsafe component")
    local = Path(local_root).expanduser().resolve()
    result = local.joinpath(*parts)
    if not ArtifactPathService._inside(local, result):
        raise UnsafePathError("mapped artifact path escapes local root")
    return result


def safe_manga_id(manga_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", manga_id.replace("/", "_"))
    value = value.strip("._-") or "manga"
    if len(value) > 120:
        value = value[:108] + "_" + hashlib.sha1(manga_id.encode("utf-8")).hexdigest()[:11]
    return value


def safe_filename(name: str, *, max_length: int = 250) -> str:
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


def existing_filename(name: str) -> str:
    """Validate an existing filename without changing its on-disk spelling."""

    if not name or "\x00" in name:
        raise UnsafePathError("empty or NUL filename")
    if Path(name).name != name or "/" in name or "\\" in name:
        raise UnsafePathError("filename must not contain path separators")
    if name in {".", ".."}:
        raise UnsafePathError("invalid filename")
    return name


def direct_archive_filename(manga_id: str, manga_name: str) -> str:
    """Build the readable direct-download filename used by the old downloader."""

    idnum = manga_id.split("/", 1)[0]
    readable_name = re.sub(r'[\\/*?:"<>|]', "_", manga_name)
    return safe_filename(f"[{idnum}]{readable_name}.zip")


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

    def torrent_gallery(self, manga_id: str) -> Path:
        """Resolve qBittorrent's complete per-gallery download directory."""
        root = self._root("torrent_download")
        folder = safe_filename(manga_id.split("/", 1)[0])
        folder_path = root / folder
        if not self._inside(root, folder_path):
            raise UnsafePathError("torrent gallery directory escapes configured root")
        if folder_path.is_symlink():
            raise UnsafePathError("torrent gallery directory is a symlink")
        return folder_path

    def torrent_registered(self, manga_id: str, filename: str) -> Path:
        """Resolve a registered artifact inside qBittorrent's per-gallery directory."""
        root = self._root("torrent_download")
        # qBittorrent has already created this file. Validate the registered
        # name for traversal, but do not truncate or otherwise rename it while
        # reconstructing its real path.
        name = existing_filename(filename)
        folder_path = self.torrent_gallery(manga_id)
        result = folder_path / name
        if not self._inside(root, result):
            raise UnsafePathError("torrent artifact escapes configured root")
        if result.is_symlink():
            raise UnsafePathError("torrent artifact path is a symlink")
        return result
