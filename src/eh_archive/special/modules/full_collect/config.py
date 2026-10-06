"""Full collection settings; no workflow state is stored in configuration files."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class FullCollectConfig:
    enabled: bool = False
    max_concurrency: int = 1
    base_url: str = ""
    batch_max_pages: int = 10
    batch_max_seconds: float = 600
    request_timeout_seconds: float = 30
    page_write_timeout_seconds: float = 30
    page_delay_min_seconds: float = 5
    page_delay_max_seconds: float = 20
    job_delay_min_seconds: float = 60
    job_delay_max_seconds: float = 120
    retry_delay_seconds: float = 2
    pool_failure_cooldown_seconds: float = 3600
    max_consecutive_failures: int = 5
    resume_after_restart: bool = True
    control_poll_seconds: float = 1


def parse_start_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("全量采集检查点时间必须是带时区的 ISO 日期时间") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("全量采集检查点时间必须包含时区")
    return parsed.astimezone(UTC)


def validate_listing_url(url: str, *, base_url: str | None = None) -> str:
    """Accept only unfiltered EH root listings on the selected HTTPS host."""
    if not isinstance(url, str) or not url or any(c.isspace() for c in url):
        raise ValueError("全量列表 URL 必须是有效的 HTTPS 地址")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.netloc in {"e-hentai.org", "exhentai.org"}
            and parts.path in {"", "/"}
            and not parts.fragment
            and parts.username is None
            and parts.password is None
            and parts.port is None
        )
        if base_url is not None:
            valid = valid and parts.netloc == urlsplit(base_url).netloc
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError("全量列表 URL 格式无效") from None
    keys = dict(pairs)
    if not valid or len(keys) != len(pairs) or {"next", "prev"}.issubset(keys):
        raise ValueError("全量列表 URL 必须为所选 EH 站点的根列表，不能含凭据或重复参数")
    fixed = {
        "f_cats": "0",
        "f_search": "",
        "f_sfl": "on",
        "f_sfu": "on",
        "f_sft": "on",
        "inline_set": "dm_e",
        "dm": "e",
    }
    for key, value in pairs:
        if key in fixed and value == fixed[key]:
            continue
        if key in {"next", "prev"} and value.isascii() and value.isdecimal() and int(value) > 0:
            continue
        if key == "seek":
            try:
                if date.fromisoformat(value).isoformat() == value:
                    continue
            except ValueError:
                pass
        raise ValueError("全量列表 URL 包含不支持的筛选或分页参数")
    return urlunsplit(("https", parts.netloc, "/", parts.query, ""))


def validate_base_url(url):
    url = validate_listing_url(url)
    if any(key in {"next", "prev", "seek"} for key, _ in parse_qsl(urlsplit(url).query)):
        raise ValueError("全量采集站点入口不得包含分页游标")
    return url


def load_full_collect_config(directory, *, app=None, secrets=None) -> FullCollectConfig:
    path = Path(directory) / "special" / "full_collect.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except tomllib.TOMLDecodeError:
        raise ValueError("special/full_collect.toml: TOML 语法错误") from None
    version = raw.pop("config_version", 2)
    if type(version) is not int or version != 2:
        raise ValueError("full_collect.config_version 必须为 2；请重启 Web / Supervisor 完成配置迁移")
    if set(raw) - {field.name for field in fields(FullCollectConfig)}:
        raise ValueError("full_collect 配置包含未知字段")
    config = FullCollectConfig(**raw)
    for name in ("enabled", "resume_after_restart"):
        if type(getattr(config, name)) is not bool:
            raise ValueError(f"full_collect.{name} 必须为布尔值")
    for name in (
        "max_concurrency",
        "batch_max_pages",
        "max_consecutive_failures",
    ):
        value = getattr(config, name)
        minimum = 1
        if type(value) is not int or value < minimum:
            raise ValueError(f"full_collect.{name} 必须是至少为 {minimum} 的整数")
    if config.max_concurrency != 1:
        raise ValueError("full_collect.max_concurrency 本期必须为 1")
    positive = {
        "batch_max_seconds",
        "request_timeout_seconds",
        "page_write_timeout_seconds",
        "pool_failure_cooldown_seconds",
        "control_poll_seconds",
    }
    for field in fields(FullCollectConfig):
        if not field.name.endswith("seconds"):
            continue
        value = getattr(config, field.name)
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            or (field.name in positive and value == 0)
        ):
            raise ValueError(f"full_collect.{field.name} 秒数无效")
    for prefix in ("page_delay", "job_delay"):
        if getattr(config, prefix + "_min_seconds") > getattr(config, prefix + "_max_seconds"):
            raise ValueError(f"full_collect.{prefix}_min_seconds 不得大于最大值")
    if not isinstance(config.base_url, str):
        raise TypeError("full_collect.base_url 必须是文本")
    if config.base_url:
        validate_base_url(config.base_url)
    if config.enabled:
        if app is None or secrets is None:
            from ....config import load_config

            app, _, _, secrets = load_config(directory)
        ZoneInfo(app.timezone)
        # 账号和站点只是新建默认值；执行时使用轮次冻结的选择。
        secrets.proxy_pool(app.full_collect_session)
    return config
