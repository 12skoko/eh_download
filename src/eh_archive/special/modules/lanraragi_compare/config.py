import math
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CompareConfig:
    enabled: bool = True
    max_concurrency: int = 1
    timeout_seconds: float = 120


def load_compare_config(directory):
    path = Path(directory) / "special" / "lanraragi_compare.toml"
    values = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = values.get("lanraragi_compare", values)
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
