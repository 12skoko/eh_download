from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time as clock_time
from pathlib import Path
from typing import Any

import tomlkit

from ..config import load_config, load_video_archive_config
from ..config.loader import DEFAULT_LOCATIONS, SUPERVISOR_MODULES

CONFIG_FILENAMES = {
    "app": "app.toml",
    "supervisor": "supervisor.toml",
    "crawl": "crawl.toml",
    "video_archive": "special/video_archive.toml",
}
_CONFIG_WRITE_LOCK = threading.Lock()
_DELETE = object()


class ConfigurationError(Exception):
    pass


class ConfigurationConflict(ConfigurationError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    path: tuple[str, ...]
    label: str
    kind: str = "text"
    editable: bool = True
    options: tuple[str, ...] = ()
    minimum: float | None = None
    help: str = ""

    @property
    def name(self) -> str:
        return "__".join(self.path)


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    kind: str
    editable: bool
    options: tuple[str, ...]
    minimum: float | None
    help: str
    value: str
    checked: bool = False


@dataclass(frozen=True)
class ConfigSection:
    name: str
    title: str
    filename: str
    revision: str
    restart: str
    fields: tuple[ConfigField, ...]


@dataclass(frozen=True)
class ConfigUpdateResult:
    filename: str
    changed_fields: tuple[str, ...]
    restart: str


APP_FIELDS = (
    FieldSpec(("timezone",), "时区"),
    FieldSpec(("log_level",), "日志级别", "choice", options=("DEBUG", "INFO", "WARNING", "ERROR")),
    FieldSpec(("log_dir",), "日志目录", editable=False),
    FieldSpec(("large_upload_threshold_bytes",), "大文件上传阈值（字节）", "int", minimum=0),
    FieldSpec(("allowed_archive_extensions",), "允许的归档扩展名", "lines"),
    FieldSpec(
        ("web_host",),
        "Web 监听地址",
        editable=False,
        help="修改监听地址需要在服务器上直接编辑并重启 Web。",
    ),
    FieldSpec(("web_port",), "Web 端口", "int", editable=False),
    FieldSpec(("qbittorrent_url",), "qBittorrent 地址", editable=False),
    FieldSpec(("qbit_torrent_path",), "qBittorrent 下载路径", editable=False),
    FieldSpec(("lanraragi_url",), "LANraragi 地址", editable=False),
    FieldSpec(("aria2_enabled",), "启用 aria2", "bool"),
    FieldSpec(("hah_enabled",), "启用 H@H", "bool"),
    FieldSpec(("fallback_method",), "备用下载方式", "choice", options=("direct", "hah", "aria2")),
    FieldSpec(("external_request_delay_seconds",), "外部请求间隔（秒）", "float", minimum=0),
    FieldSpec(("eh_request_retry_limit",), "EH 请求重试次数", "int", minimum=1),
    FieldSpec(("eh_request_retry_delay_seconds",), "EH 请求重试间隔（秒）", "float", minimum=0),
    FieldSpec(("eh_unavailable_cooldown_seconds",), "EH 不可用冷却时间（秒）", "float", minimum=0),
    FieldSpec(("sessions", "browse"), "浏览会话角色", editable=False),
    FieldSpec(("sessions", "archive"), "归档会话角色", editable=False),
    *(FieldSpec(("roots", name), f"目录：{name}", editable=False) for name in DEFAULT_LOCATIONS),
)

SUPERVISOR_FIELDS = (
    FieldSpec(("poll_seconds",), "调度轮询间隔（秒）", "float", minimum=0),
    FieldSpec(("health_check_interval_seconds",), "健康检查间隔（秒）", "float", minimum=0.001),
    FieldSpec(("collect_interval_seconds",), "采集间隔（秒）", "float", minimum=0),
    FieldSpec(("batch_size",), "批处理数量", "int", minimum=1),
    FieldSpec(("direct_download_batch_size",), "直接下载批处理数量", "int", minimum=1),
    FieldSpec(("lease_seconds",), "任务租约时长（秒）", "int", minimum=1),
    FieldSpec(("retry_limit",), "重试次数上限", "int", minimum=0),
    FieldSpec(("torrent_stall_seconds",), "Torrent 停滞判定（秒）", "int", minimum=0),
    FieldSpec(("torrent_poll_seconds",), "Torrent 轮询间隔（秒）", "float", minimum=0),
    FieldSpec(("module_restart_delay_seconds",), "组件重启间隔（秒）", "float", minimum=0),
    FieldSpec(("request_timeout_seconds",), "普通请求超时（秒）", "float", minimum=0.001),
    FieldSpec(("upload_timeout_seconds",), "上传超时（秒）", "float", minimum=0.001),
    FieldSpec(("shutdown_grace_seconds",), "停止宽限时间（秒）", "float", minimum=0),
    FieldSpec(("maintenance_start",), "维护开始时间", "time", help="留空表示不启用维护窗口。"),
    FieldSpec(("maintenance_end",), "维护结束时间", "time", help="开始和结束必须同时填写。"),
    FieldSpec(("maintenance_retry_seconds",), "维护重试间隔（秒）", "float", minimum=0),
    FieldSpec(("maintenance_recovery_timeout_seconds",), "维护恢复超时（秒）", "float", minimum=0),
    FieldSpec(("special_processing", "enabled"), "启用特殊处理调度", "bool"),
    FieldSpec(("special_processing", "poll_seconds"), "特殊任务轮询间隔（秒）", "float", minimum=0),
    FieldSpec(
        ("special_processing", "default_job_lease_seconds"),
        "特殊任务默认租约（秒）",
        "int",
        minimum=1,
    ),
    FieldSpec(("special_processing", "max_concurrency"), "特殊任务总并发", "int", minimum=1),
    *(FieldSpec(("modules", name), f"启动组件：{name}", "bool") for name in SUPERVISOR_MODULES),
    *(
        FieldSpec(("max_concurrency", name), f"最大并发：{name}", "int", minimum=1)
        for name in (
            "collect",
            "torrent_download",
            "direct_download",
            "validate",
            "prepare",
            "upload",
            "cleanup",
            "delete",
        )
    ),
)

CRAWL_FIELDS = (
    FieldSpec(("observation_days",), "观察天数", "int", minimum=0),
    FieldSpec(("collect_end_days",), "采集结束天数", "int", minimum=0),
    FieldSpec(("collect_end_offset",), "采集结束偏移", "int", minimum=0),
    FieldSpec(("collect_tags",), "采集标签", "lines", help="每行一个标签。"),
    FieldSpec(("name_keywords",), "名称关键词", "lines", help="每行一个关键词。"),
    FieldSpec(("tag_keywords",), "标签关键词", "lines", help="每行一个关键词。"),
    FieldSpec(("exclude_categories",), "排除分类", "lines", help="每行一个分类。"),
    FieldSpec(("video_markers",), "视频标记", "lines", help="每行一个标记。"),
    FieldSpec(("excluded_resolutions",), "排除分辨率", "lines", help="每行一个分辨率。"),
    FieldSpec(("tag_translation_url",), "标签翻译地址"),
    FieldSpec(("urls",), "采集地址", "mapping", help="每行使用“名称 = URL”。"),
)

VIDEO_ARCHIVE_FIELDS = (
    FieldSpec(("enabled",), "启用视频档案特殊模块", "bool"),
    FieldSpec(("auto_start",), "自动进入（固定禁用）", "bool", editable=False),
    FieldSpec(("download", "category"), "专用 qBittorrent 分类"),
    FieldSpec(("work", "workspace_root"), "转换工作根目录"),
    FieldSpec(("work", "max_concurrency"), "模块最大并发", "int", minimum=1),
    FieldSpec(("ffmpeg", "executable"), "ffmpeg 可执行文件"),
    FieldSpec(("ffmpeg", "max_workers"), "ffmpeg 并行数", "int", minimum=1),
    FieldSpec(("ffmpeg", "quality"), "WebP 质量", "int", minimum=0),
    FieldSpec(("ffmpeg", "compression_level"), "WebP 压缩等级", "int", minimum=0),
    FieldSpec(("ffmpeg", "loop"), "WebP 循环次数", "int", minimum=0),
    FieldSpec(("ffmpeg", "file_timeout_seconds"), "单文件转换超时（秒）", "float", minimum=0.001),
    FieldSpec(("ffmpeg", "max_output_bytes"), "单个 WebP 最大字节数", "int", minimum=1),
    FieldSpec(("output", "include_original_mp4"), "最终 ZIP 保留原 MP4", "bool"),
    FieldSpec(
        ("output", "layout"),
        "输出布局",
        "choice",
        options=("legacy_folders",),
    ),
    FieldSpec(("safety", "max_members"), "ZIP 最大成员数", "int", minimum=1),
    FieldSpec(("safety", "max_single_file_bytes"), "ZIP 单文件最大字节数", "int", minimum=1),
    FieldSpec(("safety", "max_expanded_bytes"), "ZIP 最大展开字节数", "int", minimum=1),
)

_SECTION_META = {
    "app": ("应用配置", "Web 和 Supervisor", APP_FIELDS),
    "supervisor": ("调度配置", "Supervisor", SUPERVISOR_FIELDS),
    "crawl": ("采集配置", "Supervisor", CRAWL_FIELDS),
}


def load_config_sections(config_dir: str | Path) -> tuple[ConfigSection, ...]:
    config_dir = Path(config_dir)
    app, supervisor, crawl, _ = load_config(config_dir)
    values = {
        "app": _app_values(app),
        "supervisor": _supervisor_values(supervisor),
        "crawl": _crawl_values(crawl),
    }
    section_meta = dict(_SECTION_META)
    video_path = config_dir / CONFIG_FILENAMES["video_archive"]
    if video_path.is_file():
        values["video_archive"] = _video_archive_values(load_video_archive_config(config_dir))
        section_meta["video_archive"] = (
            "视频档案特殊处理",
            "Supervisor 与 Web",
            VIDEO_ARCHIVE_FIELDS,
        )
    sections: list[ConfigSection] = []
    for name, (title, restart, specs) in section_meta.items():
        path = config_dir / CONFIG_FILENAMES[name]
        raw = path.read_bytes() if path.exists() else b""
        fields = tuple(_field_view(spec, _nested_value(values[name], spec.path)) for spec in specs)
        sections.append(
            ConfigSection(
                name=name,
                title=title,
                filename=CONFIG_FILENAMES[name],
                revision=_revision(raw),
                restart=restart,
                fields=fields,
            )
        )
    return tuple(sections)


def update_config_section(
    config_dir: str | Path,
    section_name: str,
    values: Mapping[str, Any],
    *,
    revision: str,
) -> ConfigUpdateResult:
    section_meta = dict(_SECTION_META)
    section_meta["video_archive"] = (
        "视频档案特殊处理",
        "Supervisor 与 Web",
        VIDEO_ARCHIVE_FIELDS,
    )
    if section_name not in section_meta:
        raise ConfigurationError("未知配置区域")
    config_dir = Path(config_dir)
    filename = CONFIG_FILENAMES[section_name]
    path = config_dir / filename
    if not path.is_file():
        raise ConfigurationError(f"配置文件不存在：{filename}")
    _, restart, specs = section_meta[section_name]

    with _CONFIG_WRITE_LOCK:
        original = path.read_bytes()
        if not revision or revision != _revision(original):
            raise ConfigurationConflict("配置文件已经被其他操作修改，请刷新页面后重试")
        try:
            document = tomlkit.parse(original.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ConfigurationError(f"无法解析 {filename}：{exc}") from exc

        changed: list[str] = []
        for spec in specs:
            if not spec.editable:
                continue
            parsed = _parse_form_value(spec, values)
            previous = _document_value(document, spec.path)
            if parsed is _DELETE:
                if previous is not _DELETE:
                    _delete_document_value(document, spec.path)
                    changed.append(spec.name)
                continue
            if _plain_value(previous) == parsed:
                continue
            _set_document_value(document, spec.path, parsed)
            changed.append(spec.name)

        if not changed:
            return ConfigUpdateResult(filename, (), restart)
        candidate = tomlkit.dumps(document)
        _validate_candidate(config_dir, filename, candidate)
        if _revision(path.read_bytes()) != revision:
            raise ConfigurationConflict("配置文件已经被其他操作修改，请刷新页面后重试")
        _atomic_replace(path, candidate)
    return ConfigUpdateResult(filename, tuple(changed), restart)


def _app_values(config) -> dict[str, Any]:
    return {
        key: getattr(config, key)
        for key in (
            "timezone",
            "log_level",
            "log_dir",
            "large_upload_threshold_bytes",
            "allowed_archive_extensions",
            "web_host",
            "web_port",
            "qbittorrent_url",
            "qbit_torrent_path",
            "lanraragi_url",
            "aria2_enabled",
            "hah_enabled",
            "fallback_method",
            "external_request_delay_seconds",
            "eh_request_retry_limit",
            "eh_request_retry_delay_seconds",
            "eh_unavailable_cooldown_seconds",
        )
    } | {
        "sessions": {
            "browse": f"account={config.browse_session.account}; network={config.browse_session.network}",
            "archive": f"account={config.archive_session.account}; network={config.archive_session.network}",
        },
        "roots": config.roots,
    }


def _supervisor_values(config) -> dict[str, Any]:
    values = {
        key: getattr(config, key)
        for key in (
            "poll_seconds",
            "health_check_interval_seconds",
            "collect_interval_seconds",
            "batch_size",
            "direct_download_batch_size",
            "lease_seconds",
            "retry_limit",
            "torrent_stall_seconds",
            "torrent_poll_seconds",
            "module_restart_delay_seconds",
            "request_timeout_seconds",
            "upload_timeout_seconds",
            "shutdown_grace_seconds",
            "maintenance_start",
            "maintenance_end",
            "maintenance_retry_seconds",
            "maintenance_recovery_timeout_seconds",
        )
    }
    values["modules"] = config.modules
    values["max_concurrency"] = config.max_concurrency
    values["special_processing"] = {
        "enabled": config.special_processing_enabled,
        "poll_seconds": config.special_processing_poll_seconds,
        "default_job_lease_seconds": config.special_job_lease_seconds,
        "max_concurrency": config.special_max_concurrency,
    }
    return values


def _video_archive_values(config) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "auto_start": config.auto_start,
        "download": {
            "category": config.download.category,
        },
        "work": {
            "workspace_root": config.work.workspace_root,
            "max_concurrency": config.work.max_concurrency,
        },
        "ffmpeg": {
            "executable": config.ffmpeg.executable,
            "max_workers": config.ffmpeg.max_workers,
            "quality": config.ffmpeg.quality,
            "compression_level": config.ffmpeg.compression_level,
            "loop": config.ffmpeg.loop,
            "file_timeout_seconds": config.ffmpeg.file_timeout_seconds,
            "max_output_bytes": config.ffmpeg.max_output_bytes,
        },
        "output": {
            "include_original_mp4": config.output.include_original_mp4,
            "layout": config.output.layout,
        },
        "safety": {
            "max_members": config.safety.max_members,
            "max_single_file_bytes": config.safety.max_single_file_bytes,
            "max_expanded_bytes": config.safety.max_expanded_bytes,
        },
    }


def _crawl_values(config) -> dict[str, Any]:
    return {
        key: getattr(config, key)
        for key in (
            "observation_days",
            "collect_end_days",
            "collect_end_offset",
            "collect_tags",
            "name_keywords",
            "tag_keywords",
            "exclude_categories",
            "video_markers",
            "excluded_resolutions",
            "tag_translation_url",
            "urls",
        )
    }


def _field_view(spec: FieldSpec, value: Any) -> ConfigField:
    checked = bool(value) if spec.kind == "bool" else False
    return ConfigField(
        name=spec.name,
        label=spec.label,
        kind=spec.kind,
        editable=spec.editable,
        options=spec.options,
        minimum=spec.minimum,
        help=spec.help,
        value=_format_field_value(value, safe=not spec.editable),
        checked=checked,
    )


def _format_field_value(value: Any, *, safe: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, clock_time):
        return value.isoformat(timespec="minutes")
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(f"{key} = {item}" for key, item in value.items())
    text_value = str(value)
    return _redact_url_credentials(text_value) if safe else text_value


def _parse_form_value(spec: FieldSpec, values: Mapping[str, Any]) -> Any:
    raw = values.get(spec.name)
    text_value = str(raw).strip() if raw is not None else ""
    try:
        if spec.kind == "bool":
            return raw is not None
        if spec.kind == "int":
            parsed: Any = int(text_value)
        elif spec.kind == "float":
            parsed = float(text_value)
        elif spec.kind == "choice":
            if text_value not in spec.options:
                raise ValueError("不是允许的选项")
            parsed = text_value
        elif spec.kind == "time":
            if not text_value:
                return _DELETE
            parsed_time = clock_time.fromisoformat(text_value)
            parsed = parsed_time.replace(microsecond=0)
        elif spec.kind == "lines":
            parsed = list(
                dict.fromkeys(line.strip() for line in text_value.splitlines() if line.strip())
            )
        elif spec.kind == "mapping":
            parsed = _parse_mapping(text_value)
        else:
            if not text_value:
                raise ValueError("不能为空")
            parsed = text_value
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{spec.label}格式无效：{exc}") from exc
    if spec.minimum is not None and isinstance(parsed, (int, float)) and parsed < spec.minimum:
        raise ConfigurationError(f"{spec.label}不能小于 {spec.minimum:g}")
    return parsed


def _parse_mapping(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_no, line in enumerate(value.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        key, separator, item = line.partition("=")
        key, item = key.strip(), item.strip()
        if not separator or not key or not item:
            raise ValueError(f"第 {line_no} 行必须使用“名称 = 值”")
        if key in result:
            raise ValueError(f"第 {line_no} 行名称重复：{key}")
        result[key] = item
    return result


def _nested_value(values: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = values
    for key in path:
        value = value[key]
    return value


def _document_value(document, path: tuple[str, ...]) -> Any:
    value: Any = document
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return _DELETE
        value = value[key]
    return value


def _plain_value(value: Any) -> Any:
    if value is _DELETE:
        return _DELETE
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


def _set_document_value(document, path: tuple[str, ...], value: Any) -> None:
    target = document
    for key in path[:-1]:
        if key not in target or not isinstance(target[key], Mapping):
            target[key] = tomlkit.table()
        target = target[key]
    target[path[-1]] = value


def _delete_document_value(document, path: tuple[str, ...]) -> None:
    target = document
    for key in path[:-1]:
        if key not in target or not isinstance(target[key], Mapping):
            return
        target = target[key]
    if path[-1] in target:
        del target[path[-1]]


def _validate_candidate(config_dir: Path, filename: str, content: str) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix=".web-config-check-", dir=config_dir) as raw_dir:
            check_dir = Path(raw_dir)
            for source_name in CONFIG_FILENAMES.values():
                source = config_dir / source_name
                if source.is_file():
                    (check_dir / source_name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, check_dir / source_name)
            (check_dir / filename).parent.mkdir(parents=True, exist_ok=True)
            (check_dir / filename).write_text(content, encoding="utf-8")
            load_config(check_dir)
            if filename == CONFIG_FILENAMES["video_archive"]:
                load_video_archive_config(check_dir)
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"配置校验失败：{exc}") from exc


def _atomic_replace(path: Path, content: str) -> None:
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, path.stat().st_mode)
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as exc:
        raise ConfigurationError(f"保存配置失败：{exc}") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _revision(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _redact_url_credentials(value: str) -> str:
    return re.sub(r"(?i)(://)[^/@\s]+@", r"\1[已隐藏]@", value)
