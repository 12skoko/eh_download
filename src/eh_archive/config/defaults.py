"""Shared sample defaults and user overrides, without modifying user files."""

import sysconfig
import tomllib
from copy import deepcopy
from pathlib import Path


def sample_directory() -> Path:
    source = Path(__file__).resolve().parents[3] / "config.sample"
    if source.is_dir():
        return source
    return Path(sysconfig.get_path("data")) / "share" / "eh_archive" / "config.sample"


def sample_values(filename: str) -> dict:
    path = sample_directory() / filename
    with path.open("rb") as handle:
        values = tomllib.load(handle)
    values.pop("config_version", None)
    # Credentials in the distributed example are instructions/placeholders,
    # never usable defaults. They must be explicitly supplied by the user.
    if filename == "secrets.toml":
        return {"web_username": values.get("web_username", "admin")}
    return values


def merge_values(defaults: dict, overrides: dict) -> dict:
    result = deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_values(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def effective_values(filename: str, overrides: dict) -> dict:
    defaults = sample_values(filename)
    # These are complete user-owned maps: deleting an entry must not resurrect
    # an example URL or an unwanted connection profile.
    for key in {"crawl.toml": ("urls",), "app.toml": ()}.get(filename, ()):
        if key in overrides:
            defaults.pop(key, None)
    if filename == "supervisor.toml":
        # CLI tasks do not migrate files. Ignore the retired table when reading
        # an old file (including update-time db commands); startup migration
        # removes it on disk. In version 3+ it is an unknown-field error.
        version = overrides.get("config_version", 0)
        if type(version) is int and version < 3:
            overrides = {key: value for key, value in overrides.items() if key != "max_concurrency"}
        # Until a service has migrated old files, CLI readers still preserve
        # the legacy timings instead of hiding them behind new defaults.
        for old, module, field in (
            ("collect_initial_delay_seconds", "collect", "initial_delay_seconds"),
            ("collect_interval_seconds", "collect", "interval_seconds"),
            ("torrent_poll_seconds", "torrent_check", "interval_seconds"),
        ):
            if old in overrides:
                defaults.setdefault("schedules", {}).setdefault(module, {})[field] = overrides[old]
    return merge_values(defaults, overrides)
