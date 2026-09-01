from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

DEFAULT_LOCATIONS = (
    "torrent_download",
    "hah_download",
    "direct_download",
    "aria2_download",
    "prepared",
    "quarantine",
    "trash",
)

SUPERVISOR_MODULES = (
    "collect",
    "screen",
    "details",
    "torrent_download",
    "direct_download",
    "validate",
    "prepare",
    "upload",
    "cleanup",
    "delete",
    "special_processing",
)
LEGACY_SUPERVISOR_MODULES = {"thumbnail"}


@dataclass(frozen=True)
class SessionRole:
    account: str = "default"
    network: str = "direct"


@dataclass(frozen=True)
class AppConfig:
    database_url: str = "postgresql+psycopg://localhost/eh_archive"
    timezone: str = "UTC"
    log_level: str = "INFO"
    log_dir: Path = Path("log")
    roots: dict[str, Path] = field(default_factory=dict)
    upload_backend: str = "auto"
    # In auto mode, files at or above this size use the filesystem backend.
    # Zero makes auto mode choose HTTP for every file.
    large_upload_threshold_bytes: int = 2 * 1024 * 1024 * 1024
    allowed_archive_extensions: tuple[str, ...] = (".zip",)
    web_host: str = "127.0.0.1"
    web_port: int = 8787
    browse_session: SessionRole = field(default_factory=SessionRole)
    archive_session: SessionRole = field(default_factory=SessionRole)
    qbittorrent_url: str = "http://127.0.0.1:8080"
    # Path as seen by the qBittorrent host; it may differ from the local
    # roots.torrent_download path when qBittorrent runs remotely.
    qbit_torrent_path: str | None = None
    lanraragi_url: str = "http://127.0.0.1:3000"
    lanraragi_smb_server: str = ""
    lanraragi_smb_port: int = 445
    lanraragi_smb_share: str = ""
    lanraragi_smb_relative_dir: str = ""
    lanraragi_smb_connection_timeout_seconds: float = 60.0
    lanraragi_smb_encrypt: bool = False
    lanraragi_import_poll_timeout_seconds: float = 600.0
    lanraragi_import_poll_interval_seconds: float = 3.0
    aria2_enabled: bool = False
    hah_enabled: bool = False
    fallback_method: str = "direct"
    # Minimum pause after one external web request before the next request
    # made by the same worker/session. LANraragi and qBittorrent clients do
    # not use RoleSession and are intentionally outside this throttle.
    external_request_delay_seconds: float = 5.0
    # E-Hentai/ExHentai network requests are retried in the shared RoleSession
    # before the site is considered unavailable.
    eh_request_retry_limit: int = 5
    eh_request_retry_delay_seconds: float = 10.0
    # After all request attempts fail, cool down only the affected submodule
    # while the Supervisor continues scheduling unrelated work. Zero disables
    # cooldown and makes the Supervisor drain and stop instead.
    eh_unavailable_cooldown_seconds: float = 6 * 60 * 60

    def root(self, location: str) -> Path:
        try:
            return self.roots[location]
        except KeyError as exc:
            raise KeyError(f"Unknown artifact location: {location}") from exc


@dataclass(frozen=True)
class SupervisorConfig:
    poll_seconds: float = 5.0
    collect_initial_delay_seconds: float = 60.0
    collect_interval_seconds: float = 3 * 60 * 60
    batch_size: int = 10
    direct_download_batch_size: int = 1
    lease_seconds: int = 900
    retry_limit: int = 5
    torrent_stall_seconds: int = 7 * 24 * 60 * 60
    torrent_poll_seconds: float = 60.0
    module_restart_delay_seconds: float = 5.0
    request_timeout_seconds: float = 30.0
    upload_timeout_seconds: float = 1800.0
    shutdown_grace_seconds: float = 30.0
    maintenance_start: clock_time | None = None
    maintenance_end: clock_time | None = None
    maintenance_retry_seconds: float = 30.0
    maintenance_recovery_timeout_seconds: float = 900.0
    health_check_interval_seconds: float = 60.0
    special_processing_enabled: bool = True
    special_processing_poll_seconds: float = 5.0
    special_job_lease_seconds: int = 900
    special_max_concurrency: int = 1
    modules: dict[str, bool] = field(
        default_factory=lambda: {name: True for name in SUPERVISOR_MODULES}
    )
    max_concurrency: dict[str, int] = field(
        default_factory=lambda: {
            "collect": 1,
            "screen": 1,
            "torrent_download": 1,
            "direct_download": 1,
            "validate": 1,
            "prepare": 1,
            "upload": 1,
            "cleanup": 1,
            "delete": 1,
        }
    )

    def batch_size_for(self, operation: str) -> int:
        if operation == "direct_download":
            return self.direct_download_batch_size
        return self.batch_size


@dataclass(frozen=True)
class CrawlConfig:
    urls: dict[str, str] = field(default_factory=dict)
    collect_tags: tuple[str, ...] = ()
    name_keywords: tuple[str, ...] = ()
    tag_keywords: tuple[str, ...] = ()
    observation_days: int = 1
    collect_end_days: int = 6
    collect_end_offset: int = 3000
    exclude_categories: tuple[str, ...] = ()
    video_markers: tuple[str, ...] = ("mp4", "video")
    excluded_resolutions: tuple[str, ...] = ("1280x", "800x", "1920x", "2560x")
    tag_translation_url: str = (
        "https://github.com/EhTagTranslation/Database/releases/latest/download/db.text.json"
    )

    def collection_urls(self) -> tuple[str, ...]:
        values = list(self.urls.values())
        values.extend(
            f"https://exhentai.org/tag/{quote_plus(tag, safe=':')}" for tag in self.collect_tags
        )
        return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class SecretsConfig:
    database_url: str | None = None
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    networks: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: dict[str, SessionRole] = field(default_factory=dict)
    qbittorrent: dict[str, Any] = field(default_factory=dict)
    lanraragi: dict[str, Any] = field(default_factory=dict)
    lanraragi_smb: dict[str, Any] = field(default_factory=dict)
    web_secret: str | None = None
    web_username: str = "admin"
    web_password_hash: str | None = None

    def cookies(self, role: SessionRole) -> dict[str, str]:
        value = self.accounts.get(role.account, {})
        cookies_str = value.get("cookies_str", "")
        if not isinstance(cookies_str, str):
            raise TypeError(f"accounts.{role.account}.cookies_str must be a string")
        return _parse_cookie_string(cookies_str, account=role.account)

    def network(self, role: SessionRole) -> dict[str, Any]:
        return dict(self.networks.get(role.network, {}))


@dataclass(frozen=True)
class VideoDownloadConfig:
    category: str


@dataclass(frozen=True)
class VideoWorkConfig:
    workspace_root: Path
    max_concurrency: int = 1


@dataclass(frozen=True)
class VideoFfmpegConfig:
    executable: Path
    max_workers: int = 2
    quality: int = 75
    compression_level: int = 6
    loop: int = 0
    file_timeout_seconds: float = 3600.0
    max_output_bytes: int = 8 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class VideoOutputConfig:
    include_original_mp4: bool = False
    layout: str = "legacy_folders"


@dataclass(frozen=True)
class VideoSafetyConfig:
    max_members: int = 100_000
    max_single_file_bytes: int = 16 * 1024 * 1024 * 1024
    max_expanded_bytes: int = 64 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class VideoArchiveConfig:
    enabled: bool
    auto_start: bool
    download: VideoDownloadConfig
    work: VideoWorkConfig
    ffmpeg: VideoFfmpegConfig
    output: VideoOutputConfig
    safety: VideoSafetyConfig

    def result_snapshot(self) -> dict[str, Any]:
        """Return only non-secret settings that determine the generated archive."""

        return {
            "include_original_mp4": self.output.include_original_mp4,
            "layout": self.output.layout,
            "webp_quality": self.ffmpeg.quality,
            "webp_compression_level": self.ffmpeg.compression_level,
            "webp_loop": self.ffmpeg.loop,
            "naming": "relative_path_with_video_prefix",
            "ordering": "zip_member_name",
        }


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _parse_cookie_string(value: str, *, account: str = "default") -> dict[str, str]:
    """Parse a browser-style ``name=value;name2=value2`` cookie string."""

    result: dict[str, str] = {}
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        name, separator, cookie_value = part.partition("=")
        name = name.strip()
        if not separator or not name:
            raise ValueError(f"accounts.{account}.cookies_str contains an invalid item: {part!r}")
        result[name] = cookie_value.strip()
    return result


def _absolute_directory(value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty absolute directory")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{key} must be an absolute directory: {value!r}")
    return path


def _config_table(value: Any, key: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{key} must be a TOML table")
    return dict(value)


def _reject_unknown_keys(value: dict[str, Any], key: str, allowed: set[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unsupported {key} entries: " + ", ".join(unknown))


def _bool_value(value: Any, key: str, *, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise TypeError(f"{key} must be true or false")
    return value


def _is_absolute_external_path(value: str) -> bool:
    """Recognize Windows and POSIX absolute paths without using local Path rules."""

    if "\x00" in value or not (
        value.startswith(("/", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        return False
    normalized = value.replace("\\", "/")
    if normalized.startswith("//"):
        # A UNC root needs both server and share names.  Empty, dot and
        # parent segments would make the qBittorrent ownership boundary
        # ambiguous on the remote host.
        tail = normalized[2:].rstrip("/")
        parts = tail.split("/") if tail else []
        return len(parts) >= 2 and all(part not in {"", ".", ".."} for part in parts)
    if re.match(r"^[A-Za-z]:/", normalized):
        tail = normalized[3:].rstrip("/")
    else:
        tail = normalized[1:].rstrip("/")
    return not tail or all(part not in {"", ".", ".."} for part in tail.split("/"))


def _path_map(raw: dict[str, Any]) -> dict[str, Path]:
    configured = raw.get("roots")
    if not isinstance(configured, dict):
        raise TypeError("app.toml must define a [roots] table with absolute directories")
    roots = dict(configured)
    missing = [key for key in DEFAULT_LOCATIONS if key not in roots]
    if missing:
        raise ValueError("app.toml is missing required [roots] entries: " + ", ".join(missing))
    unknown = sorted(set(roots) - set(DEFAULT_LOCATIONS))
    if unknown:
        raise ValueError("unsupported [roots] entries: " + ", ".join(unknown))
    return {str(key): _absolute_directory(value, f"roots.{key}") for key, value in roots.items()}


def _module_map(value: Any) -> dict[str, bool]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise TypeError("supervisor.toml [modules] must be a table")
    unknown = sorted(set(value) - set(SUPERVISOR_MODULES) - LEGACY_SUPERVISOR_MODULES)
    if unknown:
        raise ValueError("unsupported [modules] entries: " + ", ".join(unknown))
    invalid = sorted(str(key) for key, enabled in value.items() if type(enabled) is not bool)
    if invalid:
        raise TypeError("[modules] entries must be true or false: " + ", ".join(invalid))
    return {name: bool(value.get(name, True)) for name in SUPERVISOR_MODULES}


def _string_tuple(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{key} must be an array of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{key} must be an array of strings")
        item = item.strip()
        if not item:
            raise ValueError(f"{key} must not contain empty tags")
        if item not in result:
            result.append(item)
    return tuple(result)


def _optional_clock_time(value: Any, key: str) -> clock_time | None:
    if value is None or value == "":
        return None
    if isinstance(value, clock_time):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = clock_time.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"{key} must use HH:MM or HH:MM:SS") from exc
    else:
        raise TypeError(f"{key} must be a time string")
    if parsed.tzinfo is not None:
        raise ValueError(f"{key} must not include a timezone offset")
    return parsed.replace(microsecond=0)


def _role(raw: dict[str, Any], key: str) -> SessionRole:
    value = raw.get(key, {}) or {}
    return SessionRole(str(value.get("account", "default")), str(value.get("network", "direct")))


def load_video_archive_config(directory: str | Path = "config") -> VideoArchiveConfig:
    """Load the non-sensitive configuration for the video-archive module."""

    path = Path(directory) / "special" / "video_archive.toml"
    if not path.is_file():
        raise ValueError(f"special module config is missing: {path}")
    raw = _read_toml(path)
    unknown = sorted(
        set(raw) - {"enabled", "auto_start", "download", "work", "ffmpeg", "output", "safety"}
    )
    if unknown:
        raise ValueError("unsupported video_archive config sections: " + ", ".join(unknown))
    download_raw = _config_table(raw.get("download"), "video_archive.download")
    work_raw = _config_table(raw.get("work"), "video_archive.work")
    ffmpeg_raw = _config_table(raw.get("ffmpeg"), "video_archive.ffmpeg")
    output_raw = _config_table(raw.get("output"), "video_archive.output")
    safety_raw = _config_table(raw.get("safety"), "video_archive.safety")
    _reject_unknown_keys(
        download_raw,
        "video_archive.download",
        {"category"},
    )
    _reject_unknown_keys(
        work_raw,
        "video_archive.work",
        {"workspace_root", "max_concurrency"},
    )
    _reject_unknown_keys(
        ffmpeg_raw,
        "video_archive.ffmpeg",
        {
            "executable",
            "max_workers",
            "quality",
            "compression_level",
            "loop",
            "file_timeout_seconds",
            "max_output_bytes",
        },
    )
    _reject_unknown_keys(
        output_raw,
        "video_archive.output",
        {"include_original_mp4", "layout"},
    )
    _reject_unknown_keys(
        safety_raw,
        "video_archive.safety",
        {"max_members", "max_single_file_bytes", "max_expanded_bytes"},
    )
    category = str(download_raw.get("category", "")).strip()
    if not category or category == "eharchive":
        raise ValueError("download.category must be a non-empty category reserved for this module")
    workspace_root = _absolute_directory(work_raw.get("workspace_root"), "work.workspace_root")
    executable = Path(str(ffmpeg_raw.get("executable", "")).strip()).expanduser()
    if not executable.is_absolute():
        raise ValueError("ffmpeg.executable must be an absolute path")
    layout = str(output_raw.get("layout", "legacy_folders"))
    if layout != "legacy_folders":
        raise ValueError("output.layout currently only supports legacy_folders")
    enabled = _bool_value(raw.get("enabled"), "video_archive.enabled", default=True)
    auto_start = _bool_value(raw.get("auto_start"), "video_archive.auto_start", default=False)
    if auto_start:
        raise ValueError("video_archive.auto_start must remain false")
    config = VideoArchiveConfig(
        enabled=enabled,
        auto_start=False,
        download=VideoDownloadConfig(category),
        work=VideoWorkConfig(
            workspace_root,
            int(work_raw.get("max_concurrency", 1)),
        ),
        ffmpeg=VideoFfmpegConfig(
            executable,
            int(ffmpeg_raw.get("max_workers", 2)),
            int(ffmpeg_raw.get("quality", 75)),
            int(ffmpeg_raw.get("compression_level", 6)),
            int(ffmpeg_raw.get("loop", 0)),
            float(ffmpeg_raw.get("file_timeout_seconds", 3600)),
            int(ffmpeg_raw.get("max_output_bytes", 8 * 1024 * 1024 * 1024)),
        ),
        output=VideoOutputConfig(
            _bool_value(
                output_raw.get("include_original_mp4"),
                "video_archive.output.include_original_mp4",
                default=False,
            ),
            layout,
        ),
        safety=VideoSafetyConfig(
            int(safety_raw.get("max_members", 100_000)),
            int(safety_raw.get("max_single_file_bytes", 16 * 1024 * 1024 * 1024)),
            int(safety_raw.get("max_expanded_bytes", 64 * 1024 * 1024 * 1024)),
        ),
    )
    if config.work.max_concurrency <= 0 or config.ffmpeg.max_workers <= 0:
        raise ValueError("video_archive concurrency values must be greater than zero")
    if not 0 <= config.ffmpeg.quality <= 100:
        raise ValueError("ffmpeg.quality must be between 0 and 100")
    if not 0 <= config.ffmpeg.compression_level <= 6:
        raise ValueError("ffmpeg.compression_level must be between 0 and 6")
    if config.ffmpeg.file_timeout_seconds <= 0 or config.ffmpeg.max_output_bytes <= 0:
        raise ValueError("ffmpeg time and output limits must be greater than zero")
    if (
        config.safety.max_members <= 0
        or config.safety.max_single_file_bytes <= 0
        or config.safety.max_expanded_bytes <= 0
    ):
        raise ValueError("video_archive safety limits must be greater than zero")
    return config


def load_config(
    directory: str | Path = "config",
) -> tuple[AppConfig, SupervisorConfig, CrawlConfig, SecretsConfig]:
    """Load the four configuration classes with environment overrides.

    Environment variables intentionally use an explicit prefix so accidental
    process environment values cannot silently alter an installation.
    """

    directory = Path(directory)
    app_raw = _read_toml(directory / "app.toml")
    supervisor_raw = _read_toml(directory / "supervisor.toml")
    crawl_raw = _read_toml(directory / "crawl.toml")
    secrets_raw = _read_toml(directory / "secrets.toml")
    database_url = (
        os.getenv("EHARCHIVE_DATABASE_URL")
        or secrets_raw.get("database_url")
        or app_raw.get("database_url")
    )
    roots = _path_map(app_raw)
    log_dir_value = app_raw.get("log_dir")
    log_dir = (
        _absolute_directory(log_dir_value, "log_dir")
        if log_dir_value is not None
        else (Path.cwd() / "log").resolve()
    )
    qbit_torrent_path: str | None = None
    if app_raw.get("qbit_torrent_path") is not None:
        qbit_torrent_path = str(app_raw["qbit_torrent_path"]).strip()
        if not _is_absolute_external_path(qbit_torrent_path):
            raise ValueError(
                "qbit_torrent_path must be an absolute path as seen by the qBittorrent host"
            )
    app = AppConfig(
        database_url=database_url or AppConfig.database_url,
        timezone=str(app_raw.get("timezone", "UTC")),
        log_level=str(app_raw.get("log_level", "INFO")),
        log_dir=log_dir,
        roots=roots,
        upload_backend=str(app_raw.get("upload_backend", AppConfig.upload_backend)).strip().lower(),
        large_upload_threshold_bytes=int(
            app_raw.get("large_upload_threshold_bytes", AppConfig.large_upload_threshold_bytes)
        ),
        allowed_archive_extensions=tuple(app_raw.get("allowed_archive_extensions", [".zip"])),
        web_host=str(app_raw.get("web_host", "127.0.0.1")),
        web_port=int(app_raw.get("web_port", 8787)),
        browse_session=_role(app_raw.get("sessions", {}), "browse"),
        archive_session=_role(app_raw.get("sessions", {}), "archive"),
        qbittorrent_url=str(app_raw.get("qbittorrent_url", "http://127.0.0.1:8080")),
        qbit_torrent_path=qbit_torrent_path,
        lanraragi_url=str(app_raw.get("lanraragi_url", "http://127.0.0.1:3000")),
        lanraragi_smb_server=str(app_raw.get("lanraragi_smb_server", "")).strip(),
        lanraragi_smb_port=int(app_raw.get("lanraragi_smb_port", 445)),
        lanraragi_smb_share=str(app_raw.get("lanraragi_smb_share", "")).strip(),
        lanraragi_smb_relative_dir=str(
            app_raw.get("lanraragi_smb_relative_dir", "")
        ).strip(),
        lanraragi_smb_connection_timeout_seconds=float(
            app_raw.get("lanraragi_smb_connection_timeout_seconds", 60.0)
        ),
        lanraragi_smb_encrypt=_bool_value(
            app_raw.get("lanraragi_smb_encrypt"),
            "lanraragi_smb_encrypt",
            default=False,
        ),
        lanraragi_import_poll_timeout_seconds=float(
            app_raw.get("lanraragi_import_poll_timeout_seconds", 600.0)
        ),
        lanraragi_import_poll_interval_seconds=float(
            app_raw.get("lanraragi_import_poll_interval_seconds", 3.0)
        ),
        aria2_enabled=bool(app_raw.get("aria2_enabled", False)),
        hah_enabled=bool(app_raw.get("hah_enabled", False)),
        fallback_method=(
            str(app_raw.get("fallback_method", "direct"))
            if str(app_raw.get("fallback_method", "direct")) in {"direct", "hah", "aria2"}
            else "direct"
        ),
        external_request_delay_seconds=float(app_raw.get("external_request_delay_seconds", 5.0)),
        eh_request_retry_limit=int(app_raw.get("eh_request_retry_limit", 5)),
        eh_request_retry_delay_seconds=float(app_raw.get("eh_request_retry_delay_seconds", 10.0)),
        eh_unavailable_cooldown_seconds=float(
            app_raw.get("eh_unavailable_cooldown_seconds", 6 * 60 * 60)
        ),
    )
    if app.external_request_delay_seconds < 0:
        raise ValueError("external_request_delay_seconds must not be negative")
    if app.upload_backend not in {"http", "filesystem", "auto"}:
        raise ValueError("upload_backend must be http, filesystem, or auto")
    if app.large_upload_threshold_bytes < 0:
        raise ValueError("large_upload_threshold_bytes must not be negative")
    if not 1 <= app.lanraragi_smb_port <= 65535:
        raise ValueError("lanraragi_smb_port must be between 1 and 65535")
    if app.lanraragi_smb_connection_timeout_seconds <= 0:
        raise ValueError("lanraragi_smb_connection_timeout_seconds must be positive")
    if app.lanraragi_import_poll_timeout_seconds <= 0:
        raise ValueError("lanraragi_import_poll_timeout_seconds must be positive")
    if app.lanraragi_import_poll_interval_seconds <= 0:
        raise ValueError("lanraragi_import_poll_interval_seconds must be positive")
    if app.eh_request_retry_limit <= 0:
        raise ValueError("eh_request_retry_limit must be greater than zero")
    if app.eh_request_retry_delay_seconds < 0:
        raise ValueError("eh_request_retry_delay_seconds must not be negative")
    if app.eh_unavailable_cooldown_seconds < 0:
        raise ValueError("eh_unavailable_cooldown_seconds must not be negative")
    limits = dict(supervisor_raw.get("max_concurrency", {}))
    special_raw = dict(supervisor_raw.get("special_processing", {}))
    supervisor = SupervisorConfig(
        poll_seconds=float(supervisor_raw.get("poll_seconds", 5)),
        collect_initial_delay_seconds=float(
            supervisor_raw.get("collect_initial_delay_seconds", 60)
        ),
        collect_interval_seconds=float(supervisor_raw.get("collect_interval_seconds", 10800)),
        batch_size=int(supervisor_raw.get("batch_size", 10)),
        direct_download_batch_size=int(supervisor_raw.get("direct_download_batch_size", 1)),
        lease_seconds=int(supervisor_raw.get("lease_seconds", 900)),
        retry_limit=int(supervisor_raw.get("retry_limit", 5)),
        torrent_stall_seconds=int(supervisor_raw.get("torrent_stall_seconds", 7 * 24 * 60 * 60)),
        torrent_poll_seconds=float(supervisor_raw.get("torrent_poll_seconds", 60.0)),
        module_restart_delay_seconds=float(supervisor_raw.get("module_restart_delay_seconds", 5.0)),
        request_timeout_seconds=float(supervisor_raw.get("request_timeout_seconds", 30)),
        upload_timeout_seconds=float(supervisor_raw.get("upload_timeout_seconds", 1800)),
        shutdown_grace_seconds=float(supervisor_raw.get("shutdown_grace_seconds", 30)),
        maintenance_start=_optional_clock_time(
            supervisor_raw.get("maintenance_start"), "maintenance_start"
        ),
        maintenance_end=_optional_clock_time(
            supervisor_raw.get("maintenance_end"), "maintenance_end"
        ),
        maintenance_retry_seconds=float(supervisor_raw.get("maintenance_retry_seconds", 30)),
        maintenance_recovery_timeout_seconds=float(
            supervisor_raw.get("maintenance_recovery_timeout_seconds", 900)
        ),
        health_check_interval_seconds=float(
            supervisor_raw.get("health_check_interval_seconds", 60)
        ),
        special_processing_enabled=bool(special_raw.get("enabled", True)),
        special_processing_poll_seconds=float(special_raw.get("poll_seconds", 5)),
        special_job_lease_seconds=int(special_raw.get("default_job_lease_seconds", 900)),
        special_max_concurrency=int(special_raw.get("max_concurrency", 1)),
        modules=_module_map(supervisor_raw.get("modules", {})),
        max_concurrency={
            **SupervisorConfig().max_concurrency,
            **{str(k): int(v) for k, v in limits.items()},
        },
    )
    if supervisor.batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if supervisor.collect_initial_delay_seconds < 0:
        raise ValueError("collect_initial_delay_seconds must not be negative")
    if supervisor.direct_download_batch_size <= 0:
        raise ValueError("direct_download_batch_size must be greater than zero")
    if supervisor.torrent_poll_seconds < 0:
        raise ValueError("torrent_poll_seconds must not be negative")
    if supervisor.module_restart_delay_seconds < 0:
        raise ValueError("module_restart_delay_seconds must not be negative")
    if supervisor.upload_timeout_seconds <= 0:
        raise ValueError("upload_timeout_seconds must be greater than zero")
    if (supervisor.maintenance_start is None) != (supervisor.maintenance_end is None):
        raise ValueError("maintenance_start and maintenance_end must be configured together")
    if (
        supervisor.maintenance_start is not None
        and supervisor.maintenance_start == supervisor.maintenance_end
    ):
        raise ValueError("maintenance_start and maintenance_end must be different")
    if supervisor.maintenance_retry_seconds < 0:
        raise ValueError("maintenance_retry_seconds must not be negative")
    if supervisor.maintenance_recovery_timeout_seconds < 0:
        raise ValueError("maintenance_recovery_timeout_seconds must not be negative")
    if supervisor.health_check_interval_seconds <= 0:
        raise ValueError("health_check_interval_seconds must be greater than zero")
    if supervisor.special_processing_poll_seconds < 0:
        raise ValueError("special_processing.poll_seconds must not be negative")
    if supervisor.special_job_lease_seconds <= 0:
        raise ValueError("special_processing.default_job_lease_seconds must be greater than zero")
    if supervisor.special_max_concurrency <= 0:
        raise ValueError("special_processing.max_concurrency must be greater than zero")
    crawl = CrawlConfig(
        urls={str(k): str(v) for k, v in dict(crawl_raw.get("urls", {})).items()},
        collect_tags=_string_tuple(crawl_raw.get("collect_tags", []), "collect_tags"),
        name_keywords=tuple(crawl_raw.get("name_keywords", [])),
        tag_keywords=tuple(crawl_raw.get("tag_keywords", [])),
        observation_days=int(crawl_raw.get("observation_days", 1)),
        collect_end_days=int(crawl_raw.get("collect_end_days", 6)),
        collect_end_offset=int(crawl_raw.get("collect_end_offset", 3000)),
        exclude_categories=tuple(crawl_raw.get("exclude_categories", [])),
        video_markers=tuple(crawl_raw.get("video_markers", ["mp4", "video"])),
        excluded_resolutions=tuple(
            crawl_raw.get("excluded_resolutions", ["1280x", "800x", "1920x", "2560x"])
        ),
        tag_translation_url=str(
            crawl_raw.get("tag_translation_url", CrawlConfig.tag_translation_url)
        ),
    )
    sessions = {
        str(k): _role(dict(secrets_raw.get("sessions", {})), str(k))
        for k in secrets_raw.get("sessions", {})
    }
    secrets = SecretsConfig(
        database_url=database_url,
        accounts=dict(secrets_raw.get("accounts", {})),
        networks=dict(secrets_raw.get("networks", {})),
        sessions=sessions,
        qbittorrent=dict(secrets_raw.get("qbittorrent", {})),
        lanraragi=dict(secrets_raw.get("lanraragi", {})),
        lanraragi_smb=dict(secrets_raw.get("lanraragi_smb", {})),
        web_secret=os.getenv("EHARCHIVE_WEB_SECRET") or secrets_raw.get("web_secret"),
        web_username=str(
            os.getenv("EHARCHIVE_WEB_USERNAME") or secrets_raw.get("web_username") or "admin"
        ),
        web_password_hash=(
            os.getenv("EHARCHIVE_WEB_PASSWORD_HASH") or secrets_raw.get("web_password_hash")
        ),
    )
    return app, supervisor, crawl, secrets
