from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ..config import load_config
from . import ManagementError
from .config import (
    DEFAULT_CONFIG,
    LOCK_PATH,
    OPERATION_TEMPLATE,
    SUPERVISOR_UNIT,
    UPDATE_CHECK_TIMER,
    UPDATE_CHECK_UNIT,
    WEB_UNIT,
    ManagementConfig,
    load_management_config,
    write_management_config,
)
from .lock import deployment_lock
from .state import atomic_write
from .systemd import CommandRunner, Systemd, require_linux_root

UNIT_DIR = Path("/etc/systemd/system")


def quote(value) -> str:
    text = str(value)
    if any(c in text for c in "\n\r\0"):
        raise ManagementError("Invalid unit path", "invalid_path")
    return (
        '"'
        + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def directory_value(value) -> str:
    text = str(value)
    if any(c in text for c in "\n\r\0") or text != text.strip() or text.endswith("\\"):
        raise ManagementError("Invalid unit directory", "invalid_path")
    # WorkingDirectory is a literal path, not an ExecStart argument.
    return text.replace("%", "%%")


def render_units(
    config: ManagementConfig, management_path: Path = DEFAULT_CONFIG
) -> dict[str, str]:
    command = f"{quote(config.python)} -m eh_archive.management.launcher --management-config {quote(management_path)}"

    def common(description, service_type):
        return (
            f"[Unit]\nDescription=EH Archive {description}\nAfter=network.target\n\n"
            f"[Service]\nType={service_type}\nUser=root\n"
            f"WorkingDirectory={directory_value(config.repository)}\n"
            "Environment=PYTHONUNBUFFERED=1\n"
            "StandardOutput=journal\nStandardError=journal\n"
        )

    units = {}
    for name, role in ((WEB_UNIT, "web"), (SUPERVISOR_UNIT, "supervisor")):
        units[name] = (
            common(role, "simple")
            + f"ExecStart={command} {role}\nRestart=on-failure\nRestartSec=5\n"
            + ("TimeoutStopSec=infinity\n" if role == "supervisor" else "")
        )
    units[OPERATION_TEMPLATE] = (
        common("operation %i", "oneshot")
        + f"ExecStart={quote(config.python)} -m eh_archive.management.executor "
        f"--management-config {quote(management_path)} --operation-id %i\n"
        "TimeoutStartSec=infinity\nRestart=no\n"
    )
    units[UPDATE_CHECK_UNIT] = (
        common("update check", "oneshot")
        + f"ExecStart={quote(config.python)} -m eh_archive.management.update_check "
        f"--management-config {quote(management_path)}\n"
        "TimeoutStartSec=10min\nRestart=no\n"
    )
    units[UPDATE_CHECK_TIMER] = (
        "[Unit]\nDescription=EH Archive daily update check\n\n"
        f"[Timer]\nOnCalendar=*-*-* {config.update_check_time}:00\n"
        f"Persistent=true\nUnit={UPDATE_CHECK_UNIT}\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return units


def repair(
    config: ManagementConfig,
    management_path: Path = DEFAULT_CONFIG,
    runner: CommandRunner | None = None,
) -> None:
    require_linux_root()
    runner = runner or CommandRunner()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for root in config.roots:
        for name in ("history", "staging"):
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in render_units(config, management_path).items():
        atomic_write(UNIT_DIR / name, content.encode())
    runner.run(["systemd-analyze", "verify", *[UNIT_DIR / name for name in render_units(config)]])
    runner.run(["systemctl", "daemon-reload"])
    if config.update_check_enabled:
        runner.run(["systemctl", "enable", UPDATE_CHECK_TIMER])
        # A running timer must be restarted to apply a changed calendar.
        runner.run(["systemctl", "restart", UPDATE_CHECK_TIMER])
    else:
        runner.run(["systemctl", "disable", "--now", UPDATE_CHECK_TIMER])


def install(config_dir: str | Path, *, start=False, management_path: Path = DEFAULT_CONFIG):
    require_linux_root()
    for executable in ("git", "systemctl", "systemd-analyze"):
        if not shutil.which(executable):
            raise ManagementError(f"Required command not found: {executable}")
    if management_path.exists():
        raise ManagementError("Already installed; use service repair", "already_installed")
    runner = CommandRunner()
    repository = Path(runner.run(["git", "rev-parse", "--show-toplevel"])).resolve()
    config_dir = Path(config_dir).resolve()
    branch = runner.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repository)
    remote = runner.run(["git", "config", f"branch.{branch}.remote"], cwd=repository)
    if not remote or remote == ".":
        raise ManagementError("Configure an upstream remote for the current branch")
    merge = runner.run(["git", "config", f"branch.{branch}.merge"], cwd=repository)
    if merge != f"refs/heads/{branch}":
        raise ManagementError("The upstream branch must match the local branch")
    app, _, _, _ = load_config(config_dir)
    log_dir = Path(app.log_dir)
    if not log_dir.is_absolute():
        log_dir = repository / log_dir
    config = ManagementConfig(
        repository=repository,
        config_dir=config_dir,
        python=Path(sys.executable).resolve(),
        management_dir=log_dir / "management",
        branch=branch,
        remote=remote,
        web_live_url=live_url(app),
    )
    with deployment_lock():
        if management_path.exists() or any(
            (UNIT_DIR / name).exists() or (UNIT_DIR / name).is_symlink()
            for name in (
                WEB_UNIT,
                SUPERVISOR_UNIT,
                OPERATION_TEMPLATE,
                UPDATE_CHECK_UNIT,
                UPDATE_CHECK_TIMER,
            )
        ):
            raise ManagementError(
                "Existing deployment configuration or units require manual inspection",
                "already_installed",
            )
        write_management_config(config, management_path)
        repair(config, management_path, runner)
    if start:
        from .service import submit

        submit("start_all", "cli", management_path=management_path)
    return config


def live_url(app) -> str:
    host = app.web_host
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{app.web_port}/health/live"


def uninstall(management_path: Path = DEFAULT_CONFIG) -> None:
    require_linux_root()
    load_management_config(management_path)
    systemd = Systemd()
    with deployment_lock():
        from .service import ensure_idle
        from .state import OperationStore

        ensure_idle(OperationStore(load_management_config(management_path)), systemd)
        if any(systemd.active(unit) for unit in (WEB_UNIT, SUPERVISOR_UNIT)):
            raise ManagementError("Stop services before uninstalling", "operation_conflict")
        if (UNIT_DIR / UPDATE_CHECK_TIMER).exists():
            systemd.runner.run(["systemctl", "disable", "--now", UPDATE_CHECK_TIMER])
        if (UNIT_DIR / UPDATE_CHECK_UNIT).exists():
            systemd.runner.run(["systemctl", "stop", UPDATE_CHECK_UNIT])
        for name in (
            WEB_UNIT,
            SUPERVISOR_UNIT,
            OPERATION_TEMPLATE,
            UPDATE_CHECK_UNIT,
            UPDATE_CHECK_TIMER,
        ):
            if not (UNIT_DIR / name).exists() and not (UNIT_DIR / name).is_symlink():
                continue
            systemd.runner.run(["systemctl", "disable", name])
            (UNIT_DIR / name).unlink(missing_ok=True)
        systemd.runner.run(["systemctl", "daemon-reload"])
        # Preserve management configuration and history for diagnosis or repair.


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--management-config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    # Internal update step: the parent Executor owns the deployment lock.
    repair(load_management_config(args.management_config), args.management_config)
