"""Shared manual and scheduled Git checks; never installs updates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import ManagementError
from .config import DEFAULT_CONFIG, load_management_config
from .git import GitRepository
from .lock import deployment_lock
from .service import ensure_idle
from .state import OperationStore, now, read_json, write_json
from .systemd import Systemd


def saved_check(config) -> dict:
    for root in config.roots:
        try:
            value = read_json(root / "update-check.json")
            if (
                isinstance(value, dict)
                and value.get("repository") == str(config.repository)
                and value.get("remote") == config.remote
                and value.get("branch") == config.branch
            ):
                return value
        except (OSError, ValueError):
            continue
    return {}


def check_updates(config, *, scheduled=False) -> dict:
    try:
        with deployment_lock():
            ensure_idle(OperationStore(config), Systemd())
            previous = saved_check(config)
            metadata = {
                "repository": str(config.repository),
                "remote": config.remote,
                "branch": config.branch,
                "checked_at": now(),
                "last_success_at": previous.get("last_success_at"),
                "error": None,
            }
            try:
                result = GitRepository(config).inspect(fetch=True)
            except ManagementError as exc:
                write_json(
                    config.management_dir / "update-check.json",
                    {
                        **metadata,
                        "error": str(exc),
                        "error_code": exc.code,
                    },
                )
                raise
            metadata["last_success_at"] = now()
            write_json(config.management_dir / "update-check.json", {**result, **metadata})
            return {**result, "check": metadata}
    except ManagementError as exc:
        if scheduled and exc.code == "operation_conflict":
            return {"skipped": True, "reason": str(exc)}
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--management-config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    try:
        config = load_management_config(args.management_config)
        result = check_updates(config, scheduled=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ManagementError, OSError) as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
