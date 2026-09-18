from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import time as clock_time
from pathlib import Path
from typing import Any

import tomlkit

from ..config import load_config, load_video_archive_config
from ..config.defaults import effective_values, sample_values
from ..config.loader import DEFAULT_LOCATIONS, SUPERVISOR_MODULES
from ..config.validation import ConfigValueError, validate_structure
from ..management.config_migrations import configuration_lock
from ..tasks.registry import MODULES

CONFIG_FILENAMES = {
    "app": "app.toml",
    "supervisor": "supervisor.toml",
    "crawl": "crawl.toml",
    "video_archive": "special/video_archive.toml",
    "lanraragi_compare": "special/lanraragi_compare.toml",
    "download_cleanup": "special/download_cleanup.toml",
    "secrets": "secrets.toml",
}
_CONFIG_WRITE_LOCK = threading.Lock()
_DELETE = object()


class ConfigurationError(Exception):
    def __init__(self, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.fields = fields or {}


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
    maximum: float | None = None
    optional: bool = False
    secret: bool = False

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
    policy: str = ""
    group: str = "普通设置"
    advanced: bool = False
    key: str = ""
    default: str = ""
    overridden: bool = False
    maximum: float | None = None
    optional: bool = False
    secret: bool = False
    error: str = ""


@dataclass(frozen=True)
class ConfigSection:
    name: str
    title: str
    filename: str
    revision: str
    restart: str
    fields: tuple[ConfigField, ...]
    error: str = ""
    exists: bool = True


@dataclass(frozen=True)
class ConfigUpdateResult:
    filename: str
    changed_fields: tuple[str, ...]
    restart: str
    candidate: str | None = None


APP_FIELDS = (
    FieldSpec(("timezone",), "时区"),
    FieldSpec(("log_level",), "日志级别", "choice", options=("DEBUG", "INFO", "WARNING", "ERROR")),
    FieldSpec(("log_dir",), "日志目录"),
    FieldSpec(
        ("upload_backend",),
        "LANraragi 上传后端",
        "choice",
        options=("auto", "http", "filesystem"),
    ),
    FieldSpec(("large_upload_threshold_bytes",), "大文件上传阈值（字节）", "int", minimum=0),
    FieldSpec(
        ("torrent_upload_limit_kb_per_second",),
        "Torrent 上传限速（kB/s）",
        "int",
        minimum=0,
        help="仅作用于 torrent_download 提交的任务；0 表示不限制。",
    ),
    FieldSpec(("allowed_archive_extensions",), "允许的归档扩展名", "lines"),
    FieldSpec(
        ("web_host",),
        "Web 监听地址",
        help="",
    ),
    FieldSpec(("web_port",), "Web 端口", "int", minimum=1, maximum=65535),
    FieldSpec(("qbittorrent_url",), "qBittorrent 地址", editable=False),
    FieldSpec(("qbit_torrent_path",), "qBittorrent 下载路径", editable=False),
    FieldSpec(("lanraragi_url",), "LANraragi 地址", editable=False),
    FieldSpec(("lanraragi_smb_server",), "LANraragi SMB 服务器", editable=False),
    FieldSpec(("lanraragi_smb_port",), "LANraragi SMB 端口", "int", editable=False),
    FieldSpec(("lanraragi_smb_share",), "LANraragi SMB 共享", editable=False),
    FieldSpec(("lanraragi_smb_relative_dir",), "LANraragi SMB 相对目录", editable=False),
    FieldSpec(
        ("lanraragi_smb_connection_timeout_seconds",),
        "SMB 连接超时（秒）",
        "float",
        minimum=0.001,
    ),
    FieldSpec(("lanraragi_smb_encrypt",), "启用 SMB3 加密", "bool"),
    FieldSpec(
        ("lanraragi_import_poll_timeout_seconds",),
        "Shinobu 入库超时（秒）",
        "float",
        minimum=0.001,
    ),
    FieldSpec(
        ("lanraragi_import_poll_interval_seconds",),
        "Shinobu 轮询间隔（秒）",
        "float",
        minimum=0.001,
    ),
    FieldSpec(("aria2_enabled",), "启用 aria2", "bool"),
    FieldSpec(("hah_enabled",), "启用 H@H", "bool"),
    FieldSpec(
        ("fallback_method",),
        "备用下载方式",
        "choice",
        options=("direct", "hah", "aria2", "none"),
        help="none 表示 torrent 不可用时停止自动下载，并将档案转入下载受阻状态。",
    ),
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
    FieldSpec(("batch_size",), "批处理数量", "int", minimum=1),
    FieldSpec(("direct_download_batch_size",), "直接下载批处理数量", "int", minimum=1),
    FieldSpec(("lease_seconds",), "任务租约时长（秒）", "int", minimum=1),
    FieldSpec(("retry_limit",), "重试次数上限", "int", minimum=0),
    FieldSpec(("torrent_stall_seconds",), "Torrent 停滞判定（秒）", "int", minimum=0),
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
    *(
        FieldSpec(("schedules", name, key), f"{module.label}：{label}（秒）", "float", minimum=0)
        for name, module in MODULES.items()
        if module.schedule == "interval"
        for key, label in (("initial_delay_seconds", "首次延迟"), ("interval_seconds", "运行间隔"))
    ),
    *(FieldSpec(("modules", name), f"启动组件：{name}", "bool") for name in SUPERVISOR_MODULES),
)

SUPERVISOR_FIELDS = tuple(
    replace(
        spec,
        label="启用" + (MODULES[spec.path[1]].label if spec.path[1] in MODULES else "特殊处理"),
    )
    if spec.path[0] == "modules"
    else spec
    for spec in SUPERVISOR_FIELDS
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

SECRETS_FIELDS = (
    FieldSpec(("web_username",), "网页登录用户名"),
    FieldSpec(
        ("web_password_hash",),
        "网页登录密码哈希",
        secret=True,
        optional=True,
        help="填写 eharchive web-password 生成的哈希；留空保留原值。",
    ),
    FieldSpec(
        ("web_secret",),
        "Web 会话密钥",
        secret=True,
        optional=True,
        help="修改后需重新登录；留空保留原值。",
    ),
    FieldSpec(
        ("database_url",),
        "数据库连接地址",
        secret=True,
        optional=True,
        help="仅在更换数据库时填写完整连接地址；留空保留原值。",
    ),
    *(
        FieldSpec(
            (name,),
            label,
            "toml",
            secret=True,
            optional=True,
            help="填写此配置表内的 TOML 内容，将整体替换该表；留空保留原值。",
        )
        for name, label in (
            ("accounts", "站点账号与 Cookie"),
            ("networks", "代理与网络"),
            ("sessions", "会话角色覆盖"),
            ("qbittorrent", "qBittorrent 认证"),
            ("lanraragi", "LANraragi 请求头"),
            ("lanraragi_smb", "SMB 认证"),
        )
    ),
)

# Paths and connection endpoints are editable; field_group controls their placement.
APP_FIELDS = tuple(
    replace(
        spec,
        editable=True,
        optional=spec.path[0]
        in {
            "qbit_torrent_path",
            "lanraragi_smb_server",
            "lanraragi_smb_share",
            "lanraragi_smb_relative_dir",
        },
    )
    if not spec.editable and spec.path[0] != "sessions"
    else spec
    for spec in APP_FIELDS
) + (
    FieldSpec(("sessions", "browse", "account"), "浏览账号"),
    FieldSpec(("sessions", "browse", "network"), "浏览网络"),
    FieldSpec(("sessions", "archive", "account"), "归档账号"),
    FieldSpec(("sessions", "archive", "network"), "归档网络"),
)
APP_FIELDS = tuple(
    spec
    for spec in APP_FIELDS
    if spec.path not in {("sessions", "browse"), ("sessions", "archive")}
)
VIDEO_ARCHIVE_FIELDS = tuple(
    replace(spec, maximum=100 if spec.path == ("ffmpeg", "quality") else 6)
    if spec.path in {("ffmpeg", "quality"), ("ffmpeg", "compression_level")}
    else spec
    for spec in VIDEO_ARCHIVE_FIELDS
)

_SECTION_META = {
    "app": ("应用配置", "Web 和 Supervisor", APP_FIELDS),
    "supervisor": ("调度配置", "Supervisor", SUPERVISOR_FIELDS),
    "crawl": ("采集配置", "next_worker", CRAWL_FIELDS),
    "secrets": ("账号与认证", "Web 和 Supervisor", SECRETS_FIELDS),
    "video_archive": ("视频档案特殊处理", "Supervisor", VIDEO_ARCHIVE_FIELDS),
    "lanraragi_compare": (
        "LANraragi 核对",
        "Supervisor",
        (
            FieldSpec(("enabled",), "启用核对模块", "bool"),
            FieldSpec(("max_concurrency",), "最大并发", "int", minimum=1),
            FieldSpec(("timeout_seconds",), "请求超时（秒）", "float", minimum=0.001),
        ),
    ),
    "download_cleanup": (
        "下载残留清理",
        "Supervisor",
        (FieldSpec(("enabled",), "启用下载残留清理", "bool"),),
    ),
}

POLICY_LABELS = {
    "next_worker": "下次任务生效",
    "supervisor": "需重启 Supervisor",
    "web": "需重启 Web",
    "web_and_supervisor": "需重启 Web 和 Supervisor",
}


def field_group(section: str, path: tuple[str, ...]) -> tuple[str, bool]:
    key = path[0]
    if section == "app":
        if key.startswith("web_"):
            return "Web 服务", False
        if key in {
            "upload_backend",
            "large_upload_threshold_bytes",
            "fallback_method",
            "torrent_upload_limit_kb_per_second",
            "aria2_enabled",
            "hah_enabled",
        }:
            return "下载与上传", key in {"aria2_enabled", "hah_enabled", "fallback_method"}
        if key in {"timezone", "log_level", "log_dir"}:
            return "日志与时间", key in {"timezone", "log_level"}
        if key == "roots":
            return "存储目录", False
        if key in {"qbittorrent_url", "qbit_torrent_path", "lanraragi_url", "lanraragi_smb_server"}:
            return "连接与路径", False
        if key == "sessions" or key.endswith("url") or "path" in key or "smb" in key:
            return "连接与路径", True
        return "请求与处理限制", True
    if section == "supervisor":
        if key in {"modules", "schedules"}:
            return "模块开关" if key == "modules" else "定时运行", False
        if key in {"maintenance_start", "maintenance_end"}:
            return "维护窗口", False
        if key in {"batch_size", "direct_download_batch_size", "torrent_stall_seconds"}:
            return "任务处理", False
        return "特殊处理" if key == "special_processing" else "调度与重试", path[-1].endswith("_seconds")
    if section == "crawl":
        if key in {"urls", "collect_tags"}:
            return "采集来源", False
        if key in {"observation_days", "collect_end_days", "collect_end_offset"}:
            return "采集范围", False
        return "筛选规则", key in {"video_markers", "excluded_resolutions", "tag_translation_url"}
    if section == "secrets":
        return ("网页登录", False) if key.startswith("web_") else ("连接凭据", False)
    if section == "video_archive":
        return ("处理设置", False) if key in {"enabled", "work", "output"} else ("转换与限制", True)
    return "模块设置", key == "timeout_seconds"


def field_policy(section: str, field: str) -> str:
    if section == "crawl":
        return "next_worker"
    if section == "supervisor":
        if field == "health_check_interval_seconds":
            return "web_and_supervisor"
        return (
            "next_worker"
            if field
            in {
                "direct_download_batch_size",
                "retry_limit",
                "torrent_stall_seconds",
                "upload_timeout_seconds",
            }
            else "supervisor"
        )
    if section == "video_archive":
        return "supervisor" if field in {"enabled", "work__max_concurrency"} else "next_worker"
    if section in {"download_cleanup", "lanraragi_compare"}:
        return "next_worker" if field == "timeout_seconds" else "supervisor"
    if section == "secrets":
        if field.startswith("web_"):
            return "web"
        if field == "database_url":
            return "web_and_supervisor"
        return "supervisor" if field in {"qbittorrent", "lanraragi"} else "next_worker"
    if field in {"web_host", "web_port"}:
        return "web"
    if field in {"database_url", "timezone", "log_level", "log_dir"} or field.startswith("roots__"):
        return "web_and_supervisor"
    return "supervisor" if field in {
        "eh_unavailable_cooldown_seconds", "qbittorrent_url", "lanraragi_url"
    } else "next_worker"


def merged_policy(section: str, fields) -> str:
    policies = {field_policy(section, name) for name in fields}
    if "web_and_supervisor" in policies or {"web", "supervisor"} <= policies:
        return "web_and_supervisor"
    return next((p for p in ("web", "supervisor") if p in policies), "next_worker")


def _section_document(config_dir: Path, name: str):
    path = config_dir / CONFIG_FILENAMES[name]
    raw = path.read_bytes() if path.exists() else b""
    try:
        document = tomlkit.parse(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ConfigurationError(
            f"{CONFIG_FILENAMES[name]} 的 TOML 格式或编码错误，请修正原文件。"
        ) from None
    # Two special modules support both legacy flat and named-table layouts.
    table = (
        document.get(name, document)
        if name in {"download_cleanup", "lanraragi_compare"}
        else document
    )
    if not isinstance(table, Mapping):
        raise ConfigurationError(f"{CONFIG_FILENAMES[name]} 必须使用配置表。")
    defaults = sample_values(CONFIG_FILENAMES[name])
    defaults = defaults.get(name, defaults)
    if name in {"download_cleanup", "lanraragi_compare"}:
        from ..config.defaults import merge_values

        effective = merge_values(defaults, table.unwrap())
    else:
        effective = effective_values(CONFIG_FILENAMES[name], table.unwrap())
    return raw, document, table, defaults, effective


def load_config_sections(config_dir: str | Path) -> tuple[ConfigSection, ...]:
    config_dir = Path(config_dir)
    sections = []
    for name, (title, restart, specs) in _SECTION_META.items():
        path = config_dir / CONFIG_FILENAMES[name]
        if (
            name in {"video_archive", "lanraragi_compare", "download_cleanup"}
            and not path.is_file()
        ):
            continue
        try:
            raw, _, table, defaults, effective = _section_document(config_dir, name)
        except ConfigurationError as exc:
            sections.append(
                ConfigSection(name, title, CONFIG_FILENAMES[name], "", restart, (), str(exc))
            )
            continue
        fields = []
        section_error = ""
        structural_errors = {}
        if name in {"app", "supervisor", "crawl", "secrets"}:
            try:
                validate_structure(CONFIG_FILENAMES[name], effective)
            except ConfigValueError as exc:
                section_error = str(exc)
                structural_errors["__".join(exc.path)] = str(exc)
        for spec in specs:
            value = _document_value(effective, spec.path)
            value = None if value is _DELETE else value
            default = _document_value(defaults, spec.path)
            default = None if default is _DELETE else default
            group, advanced = field_group(name, spec.path)
            field = _field_view(spec, value)
            error = ""
            if value is not None and not spec.secret:
                if spec.kind == "bool" and type(value) is not bool:
                    error = "必须填写 true 或 false"
                elif spec.kind in {"int", "float"} and (
                    type(value) not in (int, float)
                    or (spec.kind == "int" and type(value) is not int)
                ):
                    error = "必须填写整数" if spec.kind == "int" else "必须填写数字"
            if not error and not spec.secret and spec.editable:
                try:
                    form_value = (
                        {} if spec.kind == "bool" and not value else {spec.name: field.value}
                    )
                    _parse_form_value(spec, form_value)
                except ConfigurationError as exc:
                    error = str(exc)
            error = structural_errors.get(spec.name, error)
            fields.append(
                replace(
                    field,
                    policy=field_policy(name, spec.name),
                    group=group,
                    advanced=advanced,
                    key=".".join(spec.path),
                    default=_format_field_value(default, safe=True),
                    overridden=_document_value(table, spec.path) is not _DELETE,
                    maximum=spec.maximum,
                    optional=spec.optional,
                    secret=spec.secret,
                    value="" if spec.secret else field.value,
                    error=error,
                )
            )
        sections.append(
            ConfigSection(
                name,
                title,
                CONFIG_FILENAMES[name],
                _revision(raw),
                restart,
                tuple(fields),
                error=section_error
                or ("配置有错误，请修正标记的字段。" if any(f.error for f in fields) else ""),
                exists=path.is_file(),
            )
        )
    return tuple(sections)


def update_config_section(
    config_dir: str | Path,
    section_name: str,
    values: Mapping[str, Any],
    *,
    revision: str,
    publish: bool = True,
) -> ConfigUpdateResult:
    section_meta = dict(_SECTION_META)
    if section_name not in section_meta:
        raise ConfigurationError("未知配置区域")
    config_dir = Path(config_dir)
    filename = CONFIG_FILENAMES[section_name]
    path = config_dir / filename
    _, _, specs = section_meta[section_name]

    with _CONFIG_WRITE_LOCK, configuration_lock(config_dir):
        original = path.read_bytes() if path.exists() else b""
        if not revision or revision != _revision(original):
            raise ConfigurationConflict("配置文件已经被其他操作修改，请刷新页面后重试")
        try:
            document = tomlkit.parse(original.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ConfigurationError(f"无法解析 {filename}，请修正原文件中的 TOML 格式。") from None
        if not path.exists():
            from ..management.config_migrations.steps import CURRENT_VERSIONS
            document["config_version"] = CURRENT_VERSIONS[filename]

        changed: list[str] = []
        errors: dict[str, str] = {}
        table = (
            document.get(section_name, document)
            if section_name in {"download_cleanup", "lanraragi_compare"}
            else document
        )
        _, _, _, _, effective = _section_document(config_dir, section_name)
        for spec in specs:
            if not spec.editable:
                continue
            reset = values.get("reset__" + spec.name) == "true" and not spec.secret
            if spec.secret and not str(values.get(spec.name, "")).strip():
                continue
            try:
                parsed = _DELETE if reset else _parse_form_value(spec, values)
            except ConfigurationError as exc:
                errors[spec.name] = str(exc)
                continue
            previous = _document_value(table, spec.path)
            if parsed is _DELETE:
                if previous is not _DELETE:
                    _delete_document_value(table, spec.path)
                    changed.append(spec.name)
                continue
            if _plain_value(previous) == parsed:
                continue
            if (
                previous is _DELETE
                and _plain_value(_document_value(effective, spec.path)) == parsed
            ):
                continue
            _set_document_value(table, spec.path, parsed)
            changed.append(spec.name)

        if errors:
            raise ConfigurationError(f"有 {len(errors)} 项配置错误，尚未保存。", errors)

        if not changed:
            return ConfigUpdateResult(filename, (), "next_worker")
        candidate = tomlkit.dumps(document)
        try:
            _validate_candidate(config_dir, filename, candidate)
        except ConfigurationError as exc:
            matching = exc.fields or {
                spec.name: str(exc)
                for spec in specs
                if spec.path[-1] in str(exc) or spec.label in str(exc)
            }
            raise ConfigurationError(str(exc), matching) from None
        if _revision(path.read_bytes() if path.exists() else b"") != revision:
            raise ConfigurationConflict("配置文件已经被其他操作修改，请刷新页面后重试")
        if publish:
            _atomic_replace(path, candidate)
    return ConfigUpdateResult(
        filename, tuple(changed), merged_policy(section_name, changed), candidate
    )


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
        elif spec.kind == "toml":
            parsed = tomlkit.parse(text_value).unwrap()
        else:
            if not text_value:
                if spec.optional:
                    return ""
                raise ValueError("不能为空")
            parsed = text_value
    except (TypeError, ValueError) as exc:
        detail = "请检查格式" if spec.secret else str(exc)
        raise ConfigurationError(f"{spec.label}格式无效：{detail}") from None
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise ConfigurationError(f"{spec.label}必须是有限数字")
    if spec.minimum is not None and isinstance(parsed, (int, float)) and parsed < spec.minimum:
        raise ConfigurationError(f"{spec.label}不能小于 {spec.minimum:g}")
    if spec.maximum is not None and isinstance(parsed, (int, float)) and parsed > spec.maximum:
        raise ConfigurationError(f"{spec.label}不能大于 {spec.maximum:g}")
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
            if (config_dir / "secrets.toml").is_file():
                shutil.copyfile(config_dir / "secrets.toml", check_dir / "secrets.toml")
            (check_dir / filename).parent.mkdir(parents=True, exist_ok=True)
            (check_dir / filename).write_bytes(content.encode("utf-8"))
            app, _, _, secrets = load_config(check_dir)
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(app.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigurationError("时区无效，请使用如 Asia/Shanghai 的时区名称。",
                                         {"timezone": "请输入有效时区，如 Asia/Shanghai。"}) from None
            if not 1 <= app.web_port <= 65535:
                raise ValueError("Web port must be between 1 and 65535")
            from .auth import valid_password_hash

            if secrets.web_password_hash:
                if not valid_password_hash(secrets.web_password_hash) or not secrets.web_secret:
                    raise ConfigurationError("Web 认证配置无效，密码哈希与会话密钥必须正确填写。",
                        {"web_password_hash": "请使用 eharchive web-password 生成有效哈希。",
                         "web_secret": "启用登录时必须填写会话密钥。"} if filename == "secrets.toml" else {})
            elif app.web_host.casefold() not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Web login is required before listening outside localhost")
            if (check_dir / CONFIG_FILENAMES["video_archive"]).is_file():
                load_video_archive_config(check_dir)
            if (check_dir / CONFIG_FILENAMES["lanraragi_compare"]).is_file():
                from ..special.modules.lanraragi_compare.config import load_compare_config

                load_compare_config(check_dir)
            if (check_dir / CONFIG_FILENAMES["download_cleanup"]).is_file():
                from ..special.modules.download_cleanup.config import capability

                capability(check_dir)
    except ConfigValueError as exc:
        fields = {"__".join(exc.path): str(exc)} if filename == exc.filename else {}
        raise ConfigurationError(str(exc), fields) from None
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"配置校验失败：{exc}") from None


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
        if path.exists():
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
