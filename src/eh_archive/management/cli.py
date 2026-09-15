from __future__ import annotations

import json

from . import ManagementError
from .config import DEFAULT_CONFIG, SUPERVISOR_UNIT, WEB_UNIT, load_management_config
from .installer import install, repair, uninstall
from .lock import deployment_lock
from .service import ensure_idle, submit
from .state import OperationStore, read_json, tail
from .systemd import CommandRunner, Systemd
from .update_check import check_updates


def add_parsers(sub):
    service = sub.add_parser("service")
    actions = service.add_subparsers(dest="service_action", required=True)
    actions.add_parser("install").add_argument("--start", action="store_true")
    for name in ("repair", "uninstall", "status"):
        actions.add_parser(name)
    for name in ("start", "stop", "restart"):
        actions.add_parser(name).add_argument(
            "target", nargs="?", default="all", choices=("web", "supervisor", "all")
        )
    actions.add_parser("logs").add_argument(
        "target", nargs="?", default="web", choices=("web", "supervisor", "operation")
    )
    update = sub.add_parser("update")
    update.add_argument("update_action", choices=("check", "apply"))
    operation = sub.add_parser("operation")
    operations = operation.add_subparsers(dest="operation_action", required=True)
    operations.add_parser("list")
    operations.add_parser("show").add_argument("operation_id")
    operations.add_parser("cancel").add_argument("operation_id")


def run(args) -> int:
    try:
        result = dispatch(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (ManagementError, OSError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'management_error')}: {exc}")
        return 1


def dispatch(args):
    if args.command == "service" and args.service_action == "install":
        return vars(install(args.config_dir, start=args.start))
    config = load_management_config()
    store = OperationStore(config)
    if args.command == "update":
        if args.update_action == "check":
            return check_updates(config)
        return submit("git_update", "cli")
    if args.command == "operation":
        if args.operation_action == "list":
            return [store.observed(state, Systemd()) for state in store.list()]
        if args.operation_action == "cancel":
            from .service import cancel

            return cancel(args.operation_id)
        path = store.locate(args.operation_id)
        return {
            **store.observed(read_json(path / "state.json"), Systemd()),
            "log": tail(path / "operation.log"),
        }
    action = args.service_action
    if action == "repair":
        with deployment_lock():
            config = load_management_config()
            store = OperationStore(config)
            ensure_idle(store, Systemd())
            repair(config)
    elif action == "uninstall":
        uninstall()
    elif action == "status":
        return {unit: Systemd().status(unit) for unit in (WEB_UNIT, SUPERVISOR_UNIT)}
    elif action == "logs":
        unit = {
            "web": WEB_UNIT,
            "supervisor": SUPERVISOR_UNIT,
            "operation": "eharchive-operation@*.service",
        }[args.target]
        output = CommandRunner().run(["journalctl", "-u", unit, "-n", "100", "--no-pager"])
        print(output)
    else:
        return submit(f"{action}_{args.target}", "cli", management_path=DEFAULT_CONFIG)
