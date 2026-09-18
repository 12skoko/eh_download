from __future__ import annotations

from pathlib import Path

from ..web.configuration import (
    CONFIG_FILENAMES,
    ConfigurationConflict,
    _revision,
    _validate_candidate,
    update_config_section,
)
from . import ManagementError
from .config_migrations import configuration_lock
from .state import atomic_write, read_json, write_json


def stage(config, operation: Path, section: str, values, revision: str) -> dict:
    result = update_config_section(
        config.config_dir, section, values, revision=revision, publish=False
    )
    staging = config.management_dir / "staging" / operation.name
    staging.mkdir(parents=True, mode=0o700)
    if result.candidate is not None:
        atomic_write(staging / "candidate.toml", result.candidate.encode())
    write_json(
        operation / "configuration.json",
        {
            "filename": result.filename,
            "original_revision": revision,
            "new_revision": _revision(result.candidate.encode()) if result.candidate else revision,
            "changed_fields": list(result.changed_fields),
            "scope": result.restart,
            "candidate": str(staging / "candidate.toml"),
            "backup": str(operation / "configuration.backup.toml"),
            "applied": False,
            "restored": False,
        },
    )
    metadata = read_json(operation / "configuration.json")
    if section == "app" and {"web_host", "web_port"} & set(result.changed_fields):
        import tomllib

        candidate = tomllib.loads(result.candidate)
        metadata["web_listener"] = {
            "host": candidate.get("web_host", "127.0.0.1"),
            "port": candidate.get("web_port", 8787),
        }
        write_json(operation / "configuration.json", metadata)
    return metadata


def publish(config, operation: Path) -> dict:
    with configuration_lock(config.config_dir):
        return _publish(config, operation)


def _publish(config, operation: Path) -> dict:
    metadata = read_json(operation / "configuration.json")
    if metadata["filename"] not in CONFIG_FILENAMES.values():
        raise ManagementError("Invalid configuration filename", "invalid_request")
    path = config.config_dir / metadata["filename"]
    original = path.read_bytes()
    if _revision(original) != metadata["original_revision"]:
        raise ConfigurationConflict("Configuration changed after this operation was submitted")
    if not metadata["changed_fields"]:
        return metadata
    candidate = Path(metadata["candidate"]).read_bytes()
    if _revision(candidate) != metadata["new_revision"]:
        raise ManagementError("Staged configuration checksum mismatch", "invalid_candidate")
    _validate_candidate(config.config_dir, metadata["filename"], candidate.decode("utf-8"))
    atomic_write(Path(metadata["backup"]), original)
    # Persist recovery intent before replacing the live file.
    metadata["applied"] = True
    write_json(operation / "configuration.json", metadata)
    atomic_write(path, candidate)
    return metadata


def restore(config, operation: Path) -> None:
    with configuration_lock(config.config_dir):
        _restore(config, operation)


def _restore(config, operation: Path) -> None:
    metadata = read_json(operation / "configuration.json")
    if not metadata["applied"] or metadata["restored"]:
        return
    path = config.config_dir / metadata["filename"]
    if _revision(path.read_bytes()) not in {
        metadata["new_revision"],
        metadata["original_revision"],
    }:
        raise ManagementError(
            "Configuration changed externally; refusing rollback", "rollback_conflict"
        )
    atomic_write(path, Path(metadata["backup"]).read_bytes())
    metadata["restored"] = True
    write_json(operation / "configuration.json", metadata)
