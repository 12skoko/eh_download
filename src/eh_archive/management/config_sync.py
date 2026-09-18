"""Merge runtime configuration during an update; backups are for manual recovery."""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import tomlkit

from ..tasks.registry import MODULES
from .state import atomic_write

RUNTIME_FILES = ("app.toml", "supervisor.toml", "crawl.toml", "secrets.toml")
# Supported options omitted or commented out in the samples. Keep these without
# inventing a default (notably the optional maintenance window).
OPTIONAL_KEYS = {
    ("app.toml",): {"allowed_archive_extensions"},
    ("supervisor.toml",): {"maintenance_start", "maintenance_end"},
    ("crawl.toml",): {"tag_translation_url"},
    ("secrets.toml",): {"sessions"},
}
# These are user-defined maps or options passed through to external clients.
OPEN_TABLES = {
    ("crawl.toml", "urls"),
    ("secrets.toml", "accounts"),
    ("secrets.toml", "networks"),
    ("secrets.toml", "sessions"),
    ("secrets.toml", "qbittorrent"),
    ("secrets.toml", "lanraragi"),
    ("secrets.toml", "lanraragi_smb"),
    ("supervisor.toml", "max_concurrency"),
}


def _merge(current, template, path, *, open_table=False):
    open_table = open_table or path in OPEN_TABLES
    if not open_table:
        allowed = set(template) | OPTIONAL_KEYS.get(path, set())
        for key in list(current):
            if key not in allowed:
                del current[key]
    for key, value in template.items():
        if key not in current:
            current[key] = deepcopy(value)
        elif isinstance(value, Mapping):
            if not isinstance(current[key], Mapping):
                raise ValueError(f"Expected configuration table: {'.'.join((*path, key))}")
            _merge(current[key], value, (*path, key), open_table=open_table)
        elif isinstance(current[key], Mapping):
            raise ValueError(f"Expected configuration value: {'.'.join((*path, key))}")


def sync_configuration(directory: Path, samples: Path, backup: Path) -> list[str]:
    """Prepare all changes, back up all affected originals, then publish changes."""
    changes = []
    files = [Path(name) for name in RUNTIME_FILES]
    # Missing special configs intentionally mean unconfigured/disabled modules.
    files.extend(
        path.relative_to(samples)
        for path in sorted((samples / "special").glob("*.toml"))
        if (directory / path.relative_to(samples)).is_file()
    )
    for relative in files:
        target = directory / relative
        original = target.read_bytes() if target.exists() else None
        current = tomlkit.parse(original.decode("utf-8") if original is not None else "")
        before = current.unwrap()
        template = tomlkit.parse((samples / relative).read_text(encoding="utf-8"))
        if relative.as_posix() == "supervisor.toml" and "schedules" in template:
            # Carry existing intervals forward before the template removes
            # legacy flat keys and fills in the new schedule tables.
            for name, module in MODULES.items():
                if name not in template["schedules"]:
                    continue
                for key, legacy in (
                    ("initial_delay_seconds", module.legacy_initial_delay_setting),
                    ("interval_seconds", module.legacy_interval_setting),
                ):
                    if legacy and legacy in current:
                        if "schedules" not in current:
                            current["schedules"] = tomlkit.table()
                        if name not in current["schedules"]:
                            current["schedules"][name] = tomlkit.table()
                        if key not in current["schedules"][name]:
                            current["schedules"][name][key] = current[legacy]
        # These loaders accept both flat and named-table configuration.
        if relative.stem in {"lanraragi_compare", "download_cleanup"}:
            name = relative.stem
            if name in template:
                template = template[name]
            if name in current:
                wrapped = tomlkit.document()
                wrapped[name] = template
                template = wrapped
        _merge(current, template, (relative.as_posix(),))
        if current.unwrap() != before or original is None:
            changes.append((relative, original, tomlkit.dumps(current).encode("utf-8")))
    if not changes:
        return []
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    for relative, original, _ in changes:
        if original is not None:
            destination = backup / relative
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            shutil.copy2(directory / relative, destination)
    # No writes to live files until every backup succeeds. Never restore on error.
    for relative, _, candidate in changes:
        atomic_write(directory / relative, candidate)
    return [relative.as_posix() for relative, _, _ in changes]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    backup = args.backup or args.directory / "backups" / datetime.now(UTC).astimezone().strftime(
        "%Y%m%d-%H%M%S-%f"
    )
    print(f"Configuration backup location: {backup}", flush=True)
    try:
        changed = sync_configuration(args.directory, args.samples, backup)
    except Exception as exc:  # noqa: BLE001 - redact parser errors at the CLI boundary
        # Parser exceptions can contain credential values; do not log their text.
        raise SystemExit(
            f"Configuration merge failed ({type(exc).__name__}); inspect files and backup at "
            f"{backup}. No automatic recovery was performed."
        ) from None
    print("Configuration files changed: " + (", ".join(changed) or "none"), flush=True)


if __name__ == "__main__":
    main()
