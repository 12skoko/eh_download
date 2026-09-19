import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ...handlers import ModuleCapability


@dataclass(frozen=True)
class MetadataConfig:
    enabled: bool = True
    max_concurrency: int = 1
    timeout_seconds: float = 30
    batch_limit: int = 500


def load_metadata_config(directory):
    path = Path(directory) / "special" / "lanraragi_metadata.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw.pop("config_version", None)
    config = MetadataConfig(**raw)
    if (
        type(config.enabled) is not bool
        or type(config.max_concurrency) is not int
        or config.max_concurrency < 1
        or type(config.batch_limit) is not int
        or not 1 <= config.batch_limit <= 5000
        or type(config.timeout_seconds) not in (int, float)
        or not math.isfinite(config.timeout_seconds)
        or not 0 < config.timeout_seconds <= 120
    ):
        raise ValueError("LANraragi 元数据配置无效")
    return config


def capability(config_dir):
    config = load_metadata_config(config_dir)
    return ModuleCapability(
        "lanraragi_metadata",
        config.enabled,
        config.max_concurrency,
        None if config.enabled else "LANraragi 元数据更新已禁用",
    )
