"""Ordered, historical config transformations. Never derive these from defaults."""

import logging
from collections.abc import MutableMapping
from pathlib import Path

import tomlkit


def establish_version(document):
    """Version 0 is an existing, unversioned configuration."""


def app_v1_to_v2(document):
    """Pin the old implicit values that differ from the new sample defaults."""
    for name, value in {
        "timezone": "UTC",
        "log_dir": str((Path.cwd() / "log").resolve()),
        "qbit_torrent_path": "",
        "lanraragi_smb_server": "",
        "lanraragi_smb_share": "",
        "lanraragi_smb_relative_dir": "",
    }.items():
        if name not in document:
            document[name] = value


def supervisor_v1_to_v2(document):
    # Keep this mapping fixed even if the live module registry changes later.
    moves = (
        ("collect_initial_delay_seconds", "collect", "initial_delay_seconds"),
        ("collect_interval_seconds", "collect", "interval_seconds"),
        ("torrent_poll_seconds", "torrent_check", "interval_seconds"),
    )
    for old, module, field in moves:
        if old not in document:
            continue
        schedules = document.setdefault("schedules", tomlkit.table())
        if not isinstance(schedules, MutableMapping):
            raise TypeError("supervisor.toml: schedules must be a table")
        schedule = schedules.setdefault(module, tomlkit.table())
        if not isinstance(schedule, MutableMapping):
            raise TypeError(f"supervisor.toml: schedules.{module} must be a table")
        # An explicit new setting always wins, including when the old one is invalid.
        if field not in schedule:
            schedule[field] = document[old]
        elif schedule[field] != document[old]:
            logging.getLogger(__name__).warning(
                "supervisor.toml: %s 与 schedules.%s.%s 同时存在且不同，保留新字段。",
                old,
                module,
                field,
            )
        del document[old]


def supervisor_v2_to_v3(document):
    """Remove the unused ordinary-module concurrency table only."""
    document.pop("max_concurrency", None)


# Keys are source versions; each function advances exactly one version.
MIGRATIONS = {
    "app.toml": {0: establish_version, 1: app_v1_to_v2},
    "supervisor.toml": {0: establish_version, 1: supervisor_v1_to_v2, 2: supervisor_v2_to_v3},
    "crawl.toml": {0: establish_version},
    "secrets.toml": {0: establish_version},
    "special/video_archive.toml": {0: establish_version},
    "special/lanraragi_compare.toml": {0: establish_version},
    "special/download_cleanup.toml": {0: establish_version},
}

CURRENT_VERSIONS = {filename: max(steps) + 1 for filename, steps in MIGRATIONS.items()}
