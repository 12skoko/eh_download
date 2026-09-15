from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from . import ManagementError

DEFAULT_CONFIG = Path("/etc/eharchive/management.toml")
LOCK_PATH = Path("/run/eharchive/deployment.lock")
WEB_UNIT = "eharchive-web.service"
SUPERVISOR_UNIT = "eharchive-supervisor.service"
OPERATION_TEMPLATE = "eharchive-operation@.service"
UPDATE_CHECK_UNIT = "eharchive-update-check.service"
UPDATE_CHECK_TIMER = "eharchive-update-check.timer"


@dataclass(frozen=True)
class ManagementConfig:
    repository: Path
    config_dir: Path
    python: Path
    management_dir: Path
    remote: str = "origin"
    branch: str = "main"
    web_live_url: str = "http://127.0.0.1:8787/health/live"
    web_start_timeout_seconds: float = 60
    supervisor_start_timeout_seconds: float = 60
    supervisor_drain_timeout_seconds: float = 0
    poll_seconds: float = 2
    history_dirs: tuple[Path, ...] = field(default_factory=tuple)
    update_check_enabled: bool = True
    update_check_time: str = "06:00"

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((self.management_dir, *self.history_dirs)))


def load_management_config(path: Path = DEFAULT_CONFIG) -> ManagementConfig:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        deployment, git, health = data["deployment"], data["git"], data.get("health", {})
        paths = {
            key: Path(deployment[key])
            for key in ("repository", "config_dir", "python", "management_dir")
        }
        if any(not value.is_absolute() for value in paths.values()):
            raise ValueError("deployment paths must be absolute")
        for key in ("remote", "branch"):
            value = git[key]
            if (
                not isinstance(value, str)
                or not value
                or value.startswith("-")
                or any(c.isspace() or ord(c) < 32 for c in value)
            ):
                raise ValueError(f"invalid Git {key}")
        if not git.get("require_clean_worktree", True) or not git.get("fast_forward_only", True):
            raise ValueError("clean worktree and fast-forward updates are required")
        expected = {
            "web_unit": WEB_UNIT,
            "supervisor_unit": SUPERVISOR_UNIT,
            "operation_unit_template": OPERATION_TEMPLATE,
        }
        if any(value != expected.get(key) for key, value in data.get("systemd", {}).items()):
            raise ValueError("only EH Archive units are supported")
        config = ManagementConfig(
            **paths,
            remote=git["remote"],
            branch=git["branch"],
            update_check_enabled=data.get("update_check", {}).get("enabled", True),
            update_check_time=data.get("update_check", {}).get("time", "06:00"),
            history_dirs=tuple(Path(p) for p in deployment.get("history_dirs", [])),
            **{
                key: health[key]
                for key in (
                    "web_live_url",
                    "web_start_timeout_seconds",
                    "supervisor_start_timeout_seconds",
                    "supervisor_drain_timeout_seconds",
                    "poll_seconds",
                )
                if key in health
            },
        )
        if (
            not isinstance(config.update_check_enabled, bool)
            or not isinstance(config.update_check_time, str)
            or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", config.update_check_time)
        ):
            raise ValueError("update_check requires boolean enabled and HH:MM time")
        if any(not p.is_absolute() for p in config.history_dirs):
            raise ValueError("history paths must be absolute")
        if (
            min(
                config.poll_seconds,
                config.web_start_timeout_seconds,
                config.supervisor_start_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("poll and startup timeouts must be positive")
        if config.supervisor_drain_timeout_seconds < 0:
            raise ValueError("drain timeout cannot be negative")
        if not all(
            math.isfinite(value)
            for value in (
                config.poll_seconds,
                config.web_start_timeout_seconds,
                config.supervisor_start_timeout_seconds,
                config.supervisor_drain_timeout_seconds,
            )
        ):
            raise ValueError("health timeouts must be finite")
        return config
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ManagementError(
            f"Cannot load management configuration: {exc}", "not_installed"
        ) from exc


def write_management_config(config: ManagementConfig, path: Path = DEFAULT_CONFIG) -> None:
    from .state import atomic_write

    data = {
        "deployment": {
            **{
                key: str(getattr(config, key))
                for key in ("repository", "config_dir", "python", "management_dir")
            },
            "history_dirs": [str(p) for p in config.history_dirs],
        },
        "git": {
            "remote": config.remote,
            "branch": config.branch,
            "require_clean_worktree": True,
            "fast_forward_only": True,
        },
        "systemd": {
            "web_unit": WEB_UNIT,
            "supervisor_unit": SUPERVISOR_UNIT,
            "operation_unit_template": OPERATION_TEMPLATE,
        },
        "health": {
            key: getattr(config, key)
            for key in (
                "web_live_url",
                "web_start_timeout_seconds",
                "supervisor_start_timeout_seconds",
                "supervisor_drain_timeout_seconds",
                "poll_seconds",
            )
        },
        "update_check": {"enabled": config.update_check_enabled, "time": config.update_check_time},
    }
    atomic_write(Path(path), tomlkit.dumps(data).encode())
