import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ....config.defaults import merge_values, sample_values


@dataclass(frozen=True)
class CompareConfig:
    enabled: bool = True
    max_concurrency: int = 1
    timeout_seconds: float = 120


def load_compare_config(directory):
    path = Path(directory) / "special" / "lanraragi_compare.toml"
    values = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    values.pop("config_version", None)
    raw = values.get("lanraragi_compare", values)
    if not isinstance(raw, dict):
        raise TypeError("LANraragi 核对配置必须是配置表")
    defaults = sample_values("special/lanraragi_compare.toml")
    raw = merge_values(defaults.get("lanraragi_compare", defaults), raw)
    if set(raw) - {"enabled", "max_concurrency", "timeout_seconds"}:
        raise ValueError("LANraragi 核对配置包含未知字段")
    config = CompareConfig(**raw)
    if (
        type(config.enabled) is not bool
        or type(config.max_concurrency) is not int
        or config.max_concurrency < 1
        or not isinstance(config.timeout_seconds, (int, float))
        or not math.isfinite(config.timeout_seconds)
        or config.timeout_seconds <= 0
    ):
        raise ValueError("LANraragi 核对配置无效")
    return config
