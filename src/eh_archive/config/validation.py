"""Structural validation shared by service loading and the configuration page."""

import math
import tomllib

from ..tasks.registry import MODULES
from .defaults import sample_directory


class ConfigValueError(ValueError):
    def __init__(self, filename: str, path: tuple[str, ...], message: str):
        self.filename = filename
        self.path = path
        super().__init__(f"{filename} · {'.'.join(path)}：{message}")


def validate_structure(filename: str, values: dict) -> None:
    with (sample_directory() / filename).open("rb") as handle:
        schema = tomllib.load(handle)
    schema.pop("config_version", None)
    if filename == "supervisor.toml":
        schema.update(
            maintenance_start="",
            maintenance_end="",
            collect_initial_delay_seconds=60,
            collect_interval_seconds=10800,
            torrent_poll_seconds=60,
        )
        schema["modules"].update({name: True for name in MODULES})
        schema["modules"]["thumbnail"] = True
        for name, module in MODULES.items():
            if module.schedule == "interval":
                schema["schedules"][name] = {"initial_delay_seconds": 0, "interval_seconds": 0}
    if filename == "secrets.toml":
        schema["sessions"] = {}
    open_tables = {"accounts", "networks", "sessions", "qbittorrent", "lanraragi", "lanraragi_smb"}

    def check(raw, template, path=()):
        for key, value in raw.items():
            location = (*path, key)
            if not path and key == "config_version":
                continue
            if key not in template:
                raise ConfigValueError(filename, location, "未知配置项，请检查字段拼写")
            expected = template[key]
            if isinstance(expected, dict):
                if not isinstance(value, dict):
                    raise ConfigValueError(filename, location, "必须是配置表")
                if filename == "secrets.toml" and key in open_tables:
                    continue
                if filename == "crawl.toml" and key == "urls":
                    if any(not isinstance(v, str) for v in value.values()):
                        raise ConfigValueError(filename, location, "采集地址必须是文本")
                    continue
                check(value, expected, location)
            elif isinstance(expected, bool):
                if type(value) is not bool:
                    raise ConfigValueError(filename, location, "必须填写 true 或 false")
            elif isinstance(expected, (int, float)):
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ConfigValueError(filename, location, "必须是有限数字")
            elif isinstance(expected, list):
                if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                    raise ConfigValueError(filename, location, "必须是文本列表")
            elif isinstance(expected, str) and not isinstance(value, str):
                if key not in {"maintenance_start", "maintenance_end"}:
                    raise ConfigValueError(filename, location, "必须是文本")

    check(values, schema)
