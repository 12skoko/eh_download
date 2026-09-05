from __future__ import annotations

import argparse
import os
import uuid

from .config import load_config
from .db import ArchiveRepository, Database
from .db.models import EventLog, MangaRecord
from .db.schema import upgrade
from .domain.states import Status
from .logging import configure_logging, get_logger

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eharchive")
    parser.add_argument("--config-dir", default="config")
    sub = parser.add_subparsers(dest="command", required=True)
    db = sub.add_parser("db")
    db.add_argument("action", choices=("upgrade", "ping"))
    collect = sub.add_parser("collect")
    collect.add_argument("url", nargs="?")
    collect.add_argument("--stop-mode", choices=("full", "automatic"))
    collect.add_argument("--end", type=int)
    task = sub.add_parser("task")
    task.add_argument(
        "operation",
        choices=(
            "screen",
            "details",
            "torrent_download",
            "direct_download",
            "validate",
            "prepare",
            "upload",
            "cleanup",
            "delete",
        ),
    )
    task.add_argument("--limit", type=int, default=None)
    sub.add_parser("supervisor")
    sub.add_parser("web")
    sub.add_parser("web-password")
    picacg = sub.add_parser("picacg")
    picacg_sub = picacg.add_subparsers(dest="picacg_action", required=True)
    picacg_import = picacg_sub.add_parser("import")
    picacg_import.add_argument("root")
    picacg_import.add_argument("--base-url", required=True)
    picacg_sub.add_parser("screen")
    special = sub.add_parser("special")
    special_kind = special.add_subparsers(dest="special_kind", required=True)
    video_archive = special_kind.add_parser("video-archive")
    video_archive.add_argument(
        "special_action",
        choices=("collect-ready", "cleanup-completed"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "web-password":
        from getpass import getpass

        from .web.auth import hash_password

        password = getpass("Web 管理员密码: ")
        confirmation = getpass("再次输入密码: ")
        if password != confirmation:
            raise SystemExit("两次输入的密码不一致")
        print(hash_password(password))
        return 0
    app, supervisor_config, crawl, secrets = load_config(args.config_dir)
    session_run_id = str(uuid.uuid4())
    component = args.command if args.command in {"supervisor", "web"} else "cli"
    main_log_path = configure_logging(
        app.log_level,
        app.log_dir,
        timezone=app.timezone,
        component=component,
        run_id=session_run_id,
    )
    database = Database(app.database_url)
    if args.command == "db":
        if args.action == "upgrade":
            upgrade(database)
            return 0
        return 0 if database.ping() else 1
    if args.command == "task":
        if args.operation == "screen":
            from .services.screening import ScreeningService

            with database.session() as session:
                result = ScreeningService(ArchiveRepository(session), crawl).run_batch(
                    args.limit
                    if args.limit is not None
                    else supervisor_config.batch_size_for("screen"),
                    actor="cli",
                )
            print(
                f"screen processed={result.processed} queued={result.queued} "
                f"filtered_out={result.filtered_out} skipped={result.skipped}"
            )
            return 0
        from .tasks.runner import TaskExecutor

        TaskExecutor(database, config_dir=args.config_dir).run_batch(args.operation, args.limit)
        return 0
    if args.command == "special":
        from .special.service import SpecialWorkflowService

        if args.special_kind == "video-archive" and args.special_action == "collect-ready":
            with database.session() as session:
                result = SpecialWorkflowService(
                    session,
                    actor="cli",
                    config_dir=args.config_dir,
                    app_config=app,
                    trigger_source="cli",
                ).dispatch_ready_checks()
            print(
                f"video-archive collect-ready found={result.found} "
                f"queued={result.queued} skipped={result.skipped}"
            )
            return 0
        if args.special_kind == "video-archive" and args.special_action == "cleanup-completed":
            with database.session() as session:
                result = SpecialWorkflowService(
                    session,
                    actor="cli",
                    config_dir=args.config_dir,
                    app_config=app,
                    trigger_source="cli",
                ).dispatch_source_cleanups()
            print(
                f"video-archive cleanup-completed found={result.found} "
                f"queued={result.queued} skipped={result.skipped}"
            )
            return 0
    if args.command == "supervisor":
        from .supervisor.app import Supervisor

        log.info(
            "supervisor started: run_id=%s pid=%s log=%s",
            session_run_id,
            os.getpid(),
            main_log_path,
        )
        try:
            Supervisor(
                database,
                config_dir=args.config_dir,
                run_id=session_run_id,
                main_log_path=main_log_path,
            ).run_forever()
        finally:
            log.info("supervisor stopped: run_id=%s pid=%s", session_run_id, os.getpid())
        return 0
    if args.command == "web":
        import uvicorn

        from .web.app import create_app

        uvicorn.run(
            create_app(database, config_dir=args.config_dir), host=app.web_host, port=app.web_port
        )
        return 0
    if args.command == "picacg":
        from .services.picacg import PicacgService, read_export_directory

        with database.session() as session:
            service = PicacgService(
                ArchiveRepository(session), base_url=getattr(args, "base_url", "")
            )
            if args.picacg_action == "import":
                service.import_entries(read_export_directory(args.root, base_url=args.base_url))
            else:
                service.screen_entries()
        return 0
    if args.command == "collect":
        url = args.url
        if not url:
            raise SystemExit("a gallery URL is required")
        with database.session() as session:
            repository = ArchiveRepository(session)
            end = args.end
            if args.stop_mode == "automatic":
                end = repository.automatic_collect_end(
                    days=crawl.collect_end_days, offset=crawl.collect_end_offset
                )
            run_id = repository.start_collect_run(
                trigger_source="cli",
                detail={
                    "config_dir": str(args.config_dir),
                    "url": url,
                    "stop_mode": args.stop_mode or "full",
                    "end": end,
                    "observation_days": crawl.observation_days,
                },
            )
            Collector(repository, app, crawl, secrets).collect_url(url, end=end)
            repository.finish_collect_run("succeeded", detail={"end": end})
        print(f"collect run_id={run_id}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
