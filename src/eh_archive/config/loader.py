from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    "thumbnail",
    "details",
    "torrent_download",
    "direct_download",
    "validate",
    "prepare",
    "upload",
    "cleanup",
    "delete",
)


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
    max_file_size: int = 20 * 1024 * 1024 * 1024
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
    aria2_enabled: bool = False
    hah_enabled: bool = False
    fallback_method: str = "direct"

    def root(self, location: str) -> Path:
        try:
            return self.roots[location]
        except KeyError as exc:
            raise KeyError(f"Unknown artifact location: {location}") from exc


@dataclass(frozen=True)
class SupervisorConfig:
    poll_seconds: float = 5.0
    collect_interval_seconds: float = 3 * 60 * 60
    batch_size: int = 10
    lease_seconds: int = 900
    lease_recovery_seconds: int = 60
    retry_limit: int = 5
    request_timeout_seconds: float = 30.0
    shutdown_grace_seconds: float = 30.0
    thumbnail_interval_seconds: float = 900.0
    modules: dict[str, bool] = field(
        default_factory=lambda: {name: True for name in SUPERVISOR_MODULES}
    )
    max_concurrency: dict[str, int] = field(
        default_factory=lambda: {
            "collect": 1,
            "torrent_download": 1,
            "direct_download": 1,
            "validate": 1,
            "prepare": 1,
            "upload": 1,
            "cleanup": 1,
            "delete": 1,
        }
    )
    torrent_stall_seconds: int = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class CrawlConfig:
    urls: dict[str, str] = field(default_factory=dict)
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


@dataclass(frozen=True)
class SecretsConfig:
    database_url: str | None = None
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    networks: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: dict[str, SessionRole] = field(default_factory=dict)
    qbittorrent: dict[str, Any] = field(default_factory=dict)
    lanraragi: dict[str, Any] = field(default_factory=dict)
    web_secret: str | None = None

    def cookies(self, role: SessionRole) -> dict[str, str]:
        value = self.accounts.get(role.account, {})
        cookies_str = value.get("cookies_str", "")
        if not isinstance(cookies_str, str):
            raise TypeError(f"accounts.{role.account}.cookies_str must be a string")
        return _parse_cookie_string(cookies_str, account=role.account)

    def network(self, role: SessionRole) -> dict[str, Any]:
        return dict(self.networks.get(role.network, {}))


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


def _is_absolute_external_path(value: str) -> bool:
    """Recognize Windows and POSIX absolute paths without using local Path rules."""

    return value.startswith(("/", "\\\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", value))


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
    unknown = sorted(set(value) - set(SUPERVISOR_MODULES))
    if unknown:
        raise ValueError("unsupported [modules] entries: " + ", ".join(unknown))
    invalid = sorted(str(key) for key, enabled in value.items() if type(enabled) is not bool)
    if invalid:
        raise TypeError("[modules] entries must be true or false: " + ", ".join(invalid))
    return {name: bool(value.get(name, True)) for name in SUPERVISOR_MODULES}


def _role(raw: dict[str, Any], key: str) -> SessionRole:
    value = raw.get(key, {}) or {}
    return SessionRole(str(value.get("account", "default")), str(value.get("network", "direct")))


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
        max_file_size=int(app_raw.get("max_file_size", AppConfig.max_file_size)),
        allowed_archive_extensions=tuple(app_raw.get("allowed_archive_extensions", [".zip"])),
        web_host=str(app_raw.get("web_host", "127.0.0.1")),
        web_port=int(app_raw.get("web_port", 8787)),
        browse_session=_role(app_raw.get("sessions", {}), "browse"),
        archive_session=_role(app_raw.get("sessions", {}), "archive"),
        qbittorrent_url=str(app_raw.get("qbittorrent_url", "http://127.0.0.1:8080")),
        qbit_torrent_path=qbit_torrent_path,
        lanraragi_url=str(app_raw.get("lanraragi_url", "http://127.0.0.1:3000")),
        aria2_enabled=bool(app_raw.get("aria2_enabled", False)),
        hah_enabled=bool(app_raw.get("hah_enabled", False)),
        fallback_method=(
            str(app_raw.get("fallback_method", "direct"))
            if str(app_raw.get("fallback_method", "direct")) in {"direct", "hah", "aria2"}
            else "direct"
        ),
    )
    limits = dict(supervisor_raw.get("max_concurrency", {}))
    supervisor = SupervisorConfig(
        poll_seconds=float(supervisor_raw.get("poll_seconds", 5)),
        collect_interval_seconds=float(supervisor_raw.get("collect_interval_seconds", 10800)),
        batch_size=int(supervisor_raw.get("batch_size", 10)),
        lease_seconds=int(supervisor_raw.get("lease_seconds", 900)),
        lease_recovery_seconds=int(supervisor_raw.get("lease_recovery_seconds", 60)),
        retry_limit=int(supervisor_raw.get("retry_limit", 5)),
        torrent_stall_seconds=int(supervisor_raw.get("torrent_stall_seconds", 7 * 24 * 60 * 60)),
        request_timeout_seconds=float(supervisor_raw.get("request_timeout_seconds", 30)),
        shutdown_grace_seconds=float(supervisor_raw.get("shutdown_grace_seconds", 30)),
        thumbnail_interval_seconds=float(supervisor_raw.get("thumbnail_interval_seconds", 900)),
        modules=_module_map(supervisor_raw.get("modules", {})),
        max_concurrency={
            **SupervisorConfig().max_concurrency,
            **{str(k): int(v) for k, v in limits.items()},
        },
    )
    crawl = CrawlConfig(
        urls={str(k): str(v) for k, v in dict(crawl_raw.get("urls", {})).items()},
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
        web_secret=os.getenv("EHARCHIVE_WEB_SECRET") or secrets_raw.get("web_secret"),
    )
    return app, supervisor, crawl, secrets
