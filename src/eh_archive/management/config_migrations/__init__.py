"""Versioned migrations, called only by Web and Supervisor startup."""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError

from ...config import load_config, load_video_archive_config
from ...config.validation import ConfigValueError
from ..state import atomic_write
from .steps import CURRENT_VERSIONS, MIGRATIONS


class ConfigMigrationError(ValueError):
    """A startup configuration problem; messages must not contain secret values."""


@contextmanager
def configuration_lock(directory: Path, *, timeout: float = 30):
    """OS-held lock, shared by processes and automatically released on exit."""
    # Do not unlink the file: replacing its inode would break mutual exclusion.
    with (directory / ".config-migration.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ConfigMigrationError(
                        "等待配置迁移锁超时，请检查另一个启动进程。"
                    ) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prepare(directory: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    originals, candidates = {}, {}
    for filename, expected in CURRENT_VERSIONS.items():
        path = directory / filename
        # Optional/missing files retain the existing loader's semantics.
        if not path.is_file():
            continue
        original = path.read_bytes()
        try:
            document = tomlkit.parse(original.decode("utf-8"))
        except ParseError as exc:
            raise ConfigMigrationError(
                f"{filename}: TOML 语法错误（第 {exc.line} 行，第 {exc.col} 列），文件未修改。"
            ) from None
        except UnicodeError:
            raise ConfigMigrationError(
                f"{filename}: TOML 格式或 UTF-8 编码错误，文件未修改。"
            ) from None
        version = document.get("config_version", 0)
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ConfigMigrationError(f"{filename}: config_version 必须是非负整数。")
        if version > expected:
            raise ConfigMigrationError(
                f"{filename}: 配置版本 {version} 高于程序支持的 {expected}，请升级程序。"
            )
        originals[filename] = original
        if version == expected:
            candidates[filename] = original
            continue
        while version < expected:
            step = MIGRATIONS[filename].get(version)
            if step is None:
                raise ConfigMigrationError(f"{filename}: 缺少版本 {version} 的迁移步骤。")
            try:
                step(document)
            except (ValueError, TypeError) as exc:
                raise ConfigMigrationError(str(exc)) from None
            version += 1
        # TOMLDocument puts scalar keys before tables, even when appended last.
        document["config_version"] = expected
        candidates[filename] = tomlkit.dumps(document).encode("utf-8")
    return originals, candidates


def _validate(directory: Path) -> None:
    # Report migrated schedule errors with their exact field, without values.
    path = directory / "supervisor.toml"
    raw = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap() if path.exists() else {}
    schedules = raw.get("schedules", {})
    if not isinstance(schedules, dict):
        raise ConfigMigrationError("supervisor.toml: schedules 必须是配置表。")
    for module, schedule in schedules.items():
        if not isinstance(schedule, dict):
            raise ConfigMigrationError(f"supervisor.toml: schedules.{module} 必须是配置表。")
        for field in ("initial_delay_seconds", "interval_seconds"):
            if field not in schedule:
                continue
            value = schedule[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ConfigMigrationError(
                    f"supervisor.toml: schedules.{module}.{field} 必须是有限的非负数字。"
                )
    try:
        app, _, _, _ = load_config(directory)
        if not 1 <= app.web_port <= 65535:
            raise ValueError("app.toml: web_port must be between 1 and 65535")
        if (directory / "special/video_archive.toml").is_file():
            load_video_archive_config(directory)
        if (directory / "special/lanraragi_compare.toml").is_file():
            from ...special.modules.lanraragi_compare.config import load_compare_config

            load_compare_config(directory)
        if (directory / "special/download_cleanup.toml").is_file():
            from ...special.modules.download_cleanup.config import capability

            capability(directory)
        if (directory / "special/manual_torrent.toml").is_file():
            from ...special.modules.manual_torrent.module import capability as manual_capability

            manual_capability(directory)
    except ConfigValueError as exc:
        raise ConfigMigrationError(str(exc)) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        # Existing loaders may embed credentials/invalid values in exceptions.
        raise ConfigMigrationError(
            "配置校验失败，请检查配置字段的类型、范围及必填项；本次迁移未写入文件。"
        ) from None


@contextmanager
def prepared_configuration(directory: Path):
    """Read-only preview for update validation; never changes live files."""
    originals, candidates = _prepare(directory)
    with tempfile.TemporaryDirectory(prefix="eharchive-config-check-") as temporary:
        check_dir = Path(temporary)
        for filename, content in candidates.items():
            target = check_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        _validate(check_dir)
        yield check_dir, originals, candidates


def migrate_configuration(directory: str | Path = "config") -> list[str]:
    """Prepare and validate everything before backing up and replacing any file."""
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ConfigMigrationError(f"配置目录不存在：{directory}")
    with configuration_lock(directory), prepared_configuration(directory) as prepared:
        _, originals, candidates = prepared
        changed = [name for name in originals if originals[name] != candidates[name]]
        if not changed:
            return []
        # Detect edits made by an editor while migration was being prepared.
        for name, content in originals.items():
            if (directory / name).read_bytes() != content:
                raise ConfigMigrationError(f"{name}: 配置在迁移期间被修改，请重新启动。")
        backup = (
            directory
            / "backups"
            / ("config-migration-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f"))
        )
        backup.mkdir(parents=True, mode=0o700, exist_ok=False)
        for name in changed:
            destination = backup / name
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            shutil.copy2(directory / name, destination)
        # Each file is replaced atomically. On an I/O failure, retain backups;
        # the per-file versions make an interrupted run safe to resume.
        try:
            for name in changed:
                atomic_write(directory / name, candidates[name])
        except OSError:
            raise ConfigMigrationError(
                f"配置写入失败，可能有部分文件已迁移；原文件备份位于 {backup}。"
            ) from None
        return changed
