"""Ordered, historical config transformations. Never derive these from defaults."""

import json
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


def supervisor_v3_to_v4(document):
    """Database downtime tolerance ends with the maintenance window."""
    document.pop("maintenance_retry_seconds", None)
    document.pop("maintenance_recovery_timeout_seconds", None)


def video_archive_v1_to_v2(document):
    """Remove the unused automatic-entry setting; entry remains manual."""
    document.pop("auto_start", None)


def full_collect_v1_to_v2(document):
    """ID intervals are provided per workflow; keep site and runtime settings."""
    for name in (
        "start_mode", "start_days_ago", "start_at", "start_url",
        "backfill_default_days_ago", "boundary_overlap_pages",
    ):
        document.pop(name, None)


def crawl_v1_to_v2(document):
    """Preserve positive keyword matches; retire the old exclusion gates."""
    from ...config.validation import ConfigValueError
    from ...domain.screen_rules import V2_ALGORITHM

    if "screening" not in document:
        document["screening"] = {}
    rules = document["screening"]
    if not isinstance(rules, MutableMapping):
        raise ConfigValueError("crawl.toml", ("screening",), "必须是配置表")
    leaves = []
    for key, field in (("name_keywords", "title"), ("tag_keywords", "tag_text")):
        values = document.pop(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError(f"crawl.toml: {key} 必须为文本列表")
        if values:
            leaves.append({"field": field, "op": "word", "values": list(values)})
    direct = {"op": "any", "children": leaves} if leaves else None
    rules.setdefault("direct_json", json.dumps(direct, ensure_ascii=False))
    rules.setdefault("algorithm_json", V2_ALGORITHM)
    for key in ("exclude_categories", "screen_required_tags", "screen_required_tags_mode"):
        document.pop(key, None)


def crawl_v2_to_v3(document):
    document.pop("collect_tags", None)


# Keys are source versions; each function advances exactly one version.
MIGRATIONS = {
    "app.toml": {0: establish_version, 1: app_v1_to_v2},
    "supervisor.toml": {
        0: establish_version,
        1: supervisor_v1_to_v2,
        2: supervisor_v2_to_v3,
        3: supervisor_v3_to_v4,
    },
    "crawl.toml": {0: establish_version, 1: crawl_v1_to_v2, 2: crawl_v2_to_v3},
    "secrets.toml": {0: establish_version},
    "special/video_archive.toml": {0: establish_version, 1: video_archive_v1_to_v2},
    "special/lanraragi_compare.toml": {0: establish_version},
    "special/download_cleanup.toml": {0: establish_version},
    "special/manual_torrent.toml": {0: establish_version},
    "special/lanraragi_metadata.toml": {0: establish_version},
    "special/full_collect.toml": {0: establish_version, 1: full_collect_v1_to_v2},
}

CURRENT_VERSIONS = {filename: max(steps) + 1 for filename, steps in MIGRATIONS.items()}
