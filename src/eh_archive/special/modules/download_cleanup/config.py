import tomllib
from pathlib import Path

from ...handlers import ModuleCapability


def capability(config_dir):
    path = Path(config_dir) / "special" / "download_cleanup.toml"
    document = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    values = document.get("download_cleanup", document)
    if set(values) - {"enabled"} or type(values.get("enabled", True)) is not bool:
        raise ValueError("下载残留清理配置无效")
    enabled = values.get("enabled", True)
    return ModuleCapability(
        "download_cleanup",
        enabled,
        1,
        None if enabled else "download_cleanup.enabled=false",
    )
