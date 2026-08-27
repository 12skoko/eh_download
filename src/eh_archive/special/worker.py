from __future__ import annotations

import argparse
import os
import re
import uuid
from pathlib import Path

from ..config import load_config
from ..db import Database
from ..domain.errors import (
    EH_SITE_UNAVAILABLE_EXIT_CODE,
    ArchiveError,
    ErrorClass,
    classify_exception,
)
from ..logging import configure_logging, get_logger, session_log_path
from .handlers import build_executor
from .repository import ClaimedSpecialJob, SpecialRepository

log = get_logger(__name__)


def _error_code(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, ArchiveError):
        return exc.info.code, exc.info.message
    module_code = getattr(exc, "code", None)
    if isinstance(module_code, str) and module_code:
        return module_code, str(exc)
    return "unexpected_special_error", str(exc) or type(exc).__name__


def _public_error_detail(value: str) -> str:
    value = re.sub(r"https?://\S+", "[URL hidden]", value, flags=re.IGNORECASE)
    value = re.sub(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s,;]+", "[path hidden]", value)
    value = re.sub(r"(?<![\w.])/(?:[^/\s]+/)+[^\s,;]*", "[path hidden]", value)
    return value[:2000]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eharchive-special-worker")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--workflow-id", type=int, required=True)
    parser.add_argument("--lease-token", required=True)
    parser.add_argument("--lease-owner", required=True)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--run-id")
    parser.add_argument("--log-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app, _, _, _ = load_config(args.config_dir)
    run_id = args.run_id or str(uuid.uuid4())
    configure_logging(
        app.log_level,
        app.log_dir,
        timezone=app.timezone,
        component="special_processing",
        run_id=run_id,
        log_file=(
            Path(args.log_path)
            if args.log_path
            else session_log_path(
                app.log_dir,
                f"special-{args.job_id}",
                timezone=app.timezone,
                run_id=run_id,
            )
        ),
    )
    database = Database(app.database_url)
    with database.session() as session:
        values = SpecialRepository(session, run_id=run_id).validate_claim(
            args.job_id,
            workflow_id=args.workflow_id,
            lease_token=args.lease_token,
            lease_owner=args.lease_owner,
        )
        if values is None:
            log.error("special worker refused stale or invalid claim: job_id=%s", args.job_id)
            return 3
        job, workflow, manga = values
        claim = ClaimedSpecialJob(
            job.id,
            workflow.id,
            manga.manga_id,
            workflow.kind,
            job.operation,
            args.lease_token,
            args.lease_owner,
            workflow.row_version,
            manga.artifact_generation,
        )
    try:
        build_executor(
            claim.kind,
            database,
            config_dir=args.config_dir,
            claim=claim,
        ).run()
    except Exception as exc:
        code, detail = _error_code(exc)
        public_detail = _public_error_detail(detail)
        classification = classify_exception(exc)
        log.exception(
            "special worker failed: job_id=%s workflow_id=%s code=%s",
            claim.job_id,
            claim.workflow_id,
            code,
        )
        try:
            with database.session() as session:
                failure_phase = (
                    "awaiting_torrent_selection" if code == "torrent_selection_stale" else "failed"
                )
                SpecialRepository(session, run_id=run_id, timezone=app.timezone).fail(
                    claim,
                    error_code=code,
                    error_detail=public_detail,
                    phase=failure_phase,
                )
        except Exception:
            log.exception("failed to persist special worker error: job_id=%s", claim.job_id)
            return 2
        if classification.code == "eh_site_unavailable":
            return EH_SITE_UNAVAILABLE_EXIT_CODE
        if classification.category == ErrorClass.SYSTEM or code in {
            "disk_full",
            "disk_space_unavailable",
        }:
            return 2
        if classification.category == ErrorClass.TEMPORARY:
            return 3
        return 0
    finally:
        database.dispose()
    log.info(
        "special worker completed: job_id=%s workflow_id=%s operation=%s pid=%s",
        claim.job_id,
        claim.workflow_id,
        claim.operation,
        os.getpid(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
