from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path, PurePosixPath

from ..config.loader import VideoArchiveConfig, VideoSafetyConfig
from ..services.paths import UnsafePathError, safe_filename
from ..services.validator.artifact import ArtifactFingerprint, validate_artifact


class VideoArchiveError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_compose_dependencies(config: VideoArchiveConfig) -> None:
    """Probe conversion-only dependencies immediately before composition."""

    if not config.ffmpeg.executable.is_file():
        raise VideoArchiveError("ffmpeg_unavailable", "ffmpeg executable does not exist")
    workspace = config.work.workspace_root
    if not workspace.is_dir():
        raise VideoArchiveError(
            "special_workspace_unavailable",
            "video conversion workspace does not exist",
        )
    try:
        with tempfile.NamedTemporaryFile(prefix=".eharchive-probe-", dir=workspace):
            pass
        shutil.disk_usage(workspace)
    except OSError as exc:
        raise VideoArchiveError(
            "special_workspace_unavailable",
            "video conversion workspace is not writable",
        ) from exc
    try:
        result = subprocess.run(
            [str(config.ffmpeg.executable), "-hide_banner", "-encoders"],
            check=False,
            capture_output=True,
            timeout=5,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoArchiveError("ffmpeg_unavailable", str(exc)) from exc
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    if result.returncode != 0 or "libwebp" not in output:
        raise VideoArchiveError(
            "ffmpeg_webp_unavailable",
            "ffmpeg does not report a libwebp encoder",
        )


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _reject_tree_links(root: Path) -> None:
    if _is_link(root):
        raise VideoArchiveError("symlink_member", f"path is a link: {root.name}")
    if not root.exists():
        return
    for item in root.rglob("*"):
        if _is_link(item):
            raise VideoArchiveError("symlink_member", f"path contains a link: {item.name}")


def unique_zip(content_path: str | Path) -> Path:
    path = Path(content_path)
    if _is_link(path):
        raise VideoArchiveError("special_content_symlink", "torrent content path is a symlink")
    if path.is_file() and path.suffix.casefold() == ".zip":
        return path
    if not path.is_dir():
        raise VideoArchiveError("special_content_missing", "torrent content path does not exist")
    _reject_tree_links(path)
    candidates = sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink() and item.suffix.casefold() == ".zip"
    )
    if len(candidates) != 1:
        raise VideoArchiveError(
            "special_zip_ambiguous",
            f"torrent content must contain exactly one ZIP, found {len(candidates)}",
        )
    return candidates[0]


def _safe_member_parts(name: str) -> tuple[str, ...]:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
        or "\x00" in normalized
    ):
        raise VideoArchiveError("unsafe_archive_path", f"unsafe ZIP member: {name}")
    try:
        return tuple(safe_filename(part, max_length=180) for part in path.parts)
    except UnsafePathError as exc:
        raise VideoArchiveError("unsafe_archive_path", f"unsafe ZIP member: {name}") from exc


def safe_extract_zip(
    source: str | Path,
    destination: str | Path,
    *,
    limits: VideoSafetyConfig,
) -> list[Path]:
    source, destination = Path(source), Path(destination)
    _reject_tree_links(destination)
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    extracted: list[Path] = []
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise VideoArchiveError("invalid_archive", f"cannot open ZIP: {source.name}") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > limits.max_members:
            raise VideoArchiveError(
                "archive_member_limit",
                f"ZIP member count {len(infos)} exceeds allowed range",
            )
        planned: list[tuple[zipfile.ZipInfo, Path]] = []
        total_size = 0
        seen: set[str] = set()
        for info in infos:
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise VideoArchiveError(
                    "symlink_member", f"ZIP contains a symbolic link: {info.filename}"
                )
            if info.file_size > limits.max_single_file_bytes:
                raise VideoArchiveError(
                    "archive_file_limit", f"ZIP member is too large: {info.filename}"
                )
            total_size += info.file_size
            if total_size > limits.max_expanded_bytes:
                raise VideoArchiveError(
                    "archive_expanded_limit", "ZIP expanded size exceeds configured maximum"
                )
            parts = _safe_member_parts(info.filename.rstrip("/"))
            if len("/".join(parts).encode("utf-8")) > 1024:
                raise VideoArchiveError(
                    "archive_path_too_long", f"ZIP member path is too long: {info.filename}"
                )
            key = "/".join(parts).casefold()
            if key in seen:
                raise VideoArchiveError(
                    "archive_name_conflict", f"ZIP contains a name collision: {info.filename}"
                )
            seen.add(key)
            target = destination.joinpath(*parts)
            try:
                target.resolve().relative_to(destination_root)
            except ValueError as exc:
                raise VideoArchiveError(
                    "unsafe_archive_path", f"ZIP member escapes destination: {info.filename}"
                ) from exc
            if info.is_dir():
                planned.append((info, target))
                continue
            planned.append((info, target))
        bad_member = archive.testzip()
        if bad_member is not None:
            raise VideoArchiveError("crc_error", f"ZIP CRC failed: {bad_member}")
        try:
            free_bytes = shutil.disk_usage(destination_root).free
        except OSError as exc:
            raise VideoArchiveError("disk_space_unavailable", str(exc)) from exc
        if free_bytes < total_size:
            raise VideoArchiveError(
                "disk_full", "not enough free space for the expanded ZIP content"
            )
        for info, target in planned:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".extracting")
            try:
                with archive.open(info) as reader, temporary.open("wb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                if temporary.stat().st_size != info.file_size:
                    raise VideoArchiveError(
                        "archive_size_mismatch", f"ZIP member size changed: {info.filename}"
                    )
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            extracted.append(target)
    return extracted


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    include: Callable[[Path], bool] | None = None,
) -> int:
    _reject_tree_links(source)
    _reject_tree_links(destination)
    count = 0
    names: set[str] = set()
    for item in sorted(source.rglob("*"), key=lambda value: value.as_posix().casefold()):
        if _is_link(item):
            raise VideoArchiveError("symlink_member", f"source contains symlink: {item.name}")
        if not item.is_file():
            continue
        if include is not None and not include(item):
            continue
        relative = item.relative_to(source)
        key = relative.as_posix().casefold()
        if key in names:
            raise VideoArchiveError("archive_name_conflict", f"source name collision: {relative}")
        names.add(key)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def prepare_deterministic_zip(
    source: str | Path,
    temporary: str | Path,
    final: str | Path,
    *,
    before_promote: Callable[[], None] | None = None,
) -> ArtifactFingerprint:
    """Build a byte-stable ZIP so a post-promotion retry can verify equality."""

    source, temporary, final = Path(source), Path(temporary), Path(final)
    if not source.is_dir():
        raise VideoArchiveError("prepare_source_missing", "video archive output is missing")
    if final.exists():
        raise FileExistsError(f"artifact generation already exists: {final}")
    _reject_tree_links(source)
    temporary.parent.mkdir(parents=True, exist_ok=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    files = sorted(
        (item for item in source.rglob("*") if item.is_file()),
        key=lambda item: (
            item.relative_to(source).as_posix().casefold(),
            item.relative_to(source).as_posix(),
        ),
    )
    if not files:
        raise VideoArchiveError("prepare_source_empty", "video archive output is empty")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for item in files:
                relative = item.relative_to(source).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                with item.open("rb") as reader, archive.open(
                    info,
                    "w",
                    force_zip64=True,
                ) as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
        fingerprint = validate_artifact(temporary, expected_kind="zip")
        if before_promote is not None:
            before_promote()
        os.replace(temporary, final)
        return replace(fingerprint, path=final)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _valid_webp(path: Path, *, require_animation: bool = True) -> bool:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(min(size, 1024 * 1024))
    except OSError:
        return False
    if size < 16 or header[:4] != b"RIFF" or header[8:12] != b"WEBP":
        return False
    return not require_animation or b"ANIM" in header or b"ANMF" in header


def _webp_relative(video_root: Path, video: Path) -> Path:
    relative = video.relative_to(video_root)
    name = safe_filename(f"0_video_{relative.stem}.webp", max_length=180)
    return relative.parent / name


def convert_mp4_files(
    video_root: str | Path,
    webp_root: str | Path,
    *,
    config: VideoArchiveConfig,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
    video_root, webp_root = Path(video_root), Path(webp_root)
    videos = sorted(
        (
            item
            for item in video_root.rglob("*")
            if item.is_file() and item.suffix.casefold() == ".mp4"
        ),
        key=lambda value: value.relative_to(video_root).as_posix().casefold(),
    )
    if not videos:
        raise VideoArchiveError("video_file_missing", "selected video ZIP contains no MP4 files")
    outputs: dict[str, tuple[Path, Path]] = {}
    for video in videos:
        relative = _webp_relative(video_root, video)
        key = relative.as_posix().casefold()
        if key in outputs:
            raise VideoArchiveError(
                "webp_name_conflict", f"multiple videos map to {relative.as_posix()}"
            )
        outputs[key] = (video, webp_root / relative)

    def convert(pair: tuple[Path, Path]) -> Path:
        source, output = pair
        if _valid_webp(output):
            return output
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.stem + ".part.webp")
        temporary.unlink(missing_ok=True)
        command = [
            str(config.ffmpeg.executable),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vcodec",
            "libwebp",
            "-qscale",
            str(config.ffmpeg.quality),
            "-compression_level",
            str(config.ffmpeg.compression_level),
            "-loop",
            str(config.ffmpeg.loop),
            "-an",
            str(temporary),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=config.ffmpeg.file_timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            temporary.unlink(missing_ok=True)
            raise VideoArchiveError(
                "ffmpeg_timeout", f"ffmpeg timed out for {source.name}"
            ) from exc
        if result.returncode != 0:
            temporary.unlink(missing_ok=True)
            summary = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise VideoArchiveError("ffmpeg_failed", f"ffmpeg failed for {source.name}: {summary}")
        if not temporary.is_file():
            raise VideoArchiveError(
                "ffmpeg_output_missing", f"ffmpeg did not create WebP for {source.name}"
            )
        if temporary.stat().st_size > config.ffmpeg.max_output_bytes:
            temporary.unlink(missing_ok=True)
            raise VideoArchiveError("webp_too_large", f"WebP exceeds limit: {source.name}")
        if not _valid_webp(temporary):
            temporary.unlink(missing_ok=True)
            raise VideoArchiveError("invalid_webp", f"ffmpeg produced invalid WebP: {source.name}")
        os.replace(temporary, output)
        return output

    completed: list[Path] = []
    with ThreadPoolExecutor(max_workers=config.ffmpeg.max_workers) as pool:
        futures = {pool.submit(convert, pair): pair[0] for pair in outputs.values()}
        try:
            for future in as_completed(futures):
                output = future.result()
                completed.append(output)
                if progress:
                    progress(len(completed), len(videos), futures[future].name)
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return sorted(completed)


def build_legacy_layout(
    image_root: str | Path,
    video_root: str | Path,
    output_root: str | Path,
    *,
    config: VideoArchiveConfig,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, int]:
    image_root, video_root, output_root = Path(image_root), Path(video_root), Path(output_root)
    pic_count = _copy_tree(image_root, output_root / "2_pic")
    if pic_count == 0:
        raise VideoArchiveError("image_file_missing", "selected image ZIP contains no files")
    webps = convert_mp4_files(
        video_root,
        output_root / "1_webp",
        config=config,
        progress=progress,
    )
    video_count = 0
    if config.output.include_original_mp4:
        video_count = _copy_tree(
            video_root,
            output_root / "3_video",
            include=lambda item: item.suffix.casefold() == ".mp4",
        )
    return {"pictures": pic_count, "webps": len(webps), "original_video_files": video_count}
