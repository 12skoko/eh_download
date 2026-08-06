"""Requeue the reversible status results of one completed collect run."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select

from eh_archive.config import load_config
from eh_archive.db import Database
from eh_archive.db.models import EventLog, JobAttempt, MangaRecord
from eh_archive.db.repository import utcnow
from eh_archive.domain.states import Status

ROLLBACK_STATUSES = frozenset(
    {
        Status.DISCOVERED.value,
        Status.DOWNLOAD_PENDING.value,
        Status.SKIPPED.value,
    }
)
SAMPLE_LIMIT = 50


def _sample(values) -> list[str]:
    return sorted(str(value) for value in values)[:SAMPLE_LIMIT]


def _business_effects(row: MangaRecord) -> list[str]:
    fields = (
        "active_attempt_id",
        "external_download_id",
        "artifact_location",
        "artifact_filename",
        "artifact_kind",
        "artifact_generation",
        "artifact_size",
        "artifact_sha1",
        "lrr_archive_id",
        "lease_token",
        "lease_owner",
        "lease_until",
    )
    return [field for field in fields if getattr(row, field) is not None]


def _run_events(
    session, run_id: str, *, lock: bool = False
) -> tuple[EventLog | None, EventLog | None, bool]:
    started_query = (
        select(EventLog)
        .where(
            EventLog.run_id == run_id,
            EventLog.event_type == "collect_started",
            EventLog.operation == "collect",
        )
        .order_by(EventLog.id)
        .limit(1)
    )
    if lock:
        started_query = started_query.with_for_update()
    started = session.scalar(started_query)
    finished = session.scalar(
        select(EventLog)
        .where(
            EventLog.run_id == run_id,
            EventLog.event_type == "collect_finished",
            EventLog.operation == "collect",
        )
        .order_by(EventLog.id.desc())
        .limit(1)
    )
    rolled_back = (
        session.scalar(
            select(EventLog.id)
            .where(
                EventLog.run_id == run_id,
                EventLog.event_type == "collect_rollback_finished",
            )
            .limit(1)
        )
        is not None
    )
    return started, finished, rolled_back


def rollback_collect(database: Database, run_id: str, *, apply: bool) -> dict[str, Any]:
    with database.session() as session:
        started, finished, rolled_back = _run_events(session, run_id, lock=apply)
        if started is None or finished is None:
            return {
                "run_id": run_id,
                "result": "blocked",
                "reason": "not_a_completed_collect_run",
            }
        if (finished.detail or {}).get("status") != "succeeded":
            return {
                "run_id": run_id,
                "result": "blocked",
                "reason": "collect_did_not_succeed",
            }
        if rolled_back:
            return {
                "run_id": run_id,
                "result": "blocked",
                "reason": "collect_run_already_rolled_back",
            }

        # collect_touched covers every parsed row, including metadata-only
        # refreshes. status_changed also includes related rows changed by the
        # final screenall pass.
        candidate_query = (
            select(EventLog.manga_id)
            .where(
                EventLog.run_id == run_id,
                EventLog.manga_id.is_not(None),
                EventLog.event_type.in_(("collect_touched", "status_changed")),
            )
            .distinct()
        )
        candidate_ids = set(session.scalars(candidate_query))
        row_query = select(MangaRecord).where(MangaRecord.manga_id.in_(candidate_query))
        if apply:
            row_query = row_query.with_for_update()
        rows = list(session.scalars(row_query))
        attempted_ids = set(
            session.scalars(
                select(JobAttempt.manga_id)
                .where(
                    JobAttempt.manga_id.in_(candidate_query),
                    JobAttempt.started_at >= started.created_at,
                )
                .distinct()
            )
        )

        resettable: list[MangaRecord] = []
        protected: dict[str, list[str]] = {}
        skipped_statuses: Counter[str] = Counter()
        for row in rows:
            if row.status not in ROLLBACK_STATUSES:
                skipped_statuses[row.status] += 1
                continue
            effects = _business_effects(row)
            if row.manga_id in attempted_ids:
                effects.append("job_attempt_after_collect_started")
            if effects:
                protected[row.manga_id] = effects
                continue
            resettable.append(row)

        missing_ids = candidate_ids - {row.manga_id for row in rows}
        report: dict[str, Any] = {
            "run_id": run_id,
            "operation": "collect",
            "collect_started_at": started.created_at.isoformat(),
            "collect_finished_at": finished.created_at.isoformat(),
            "apply_requested": apply,
            "status_scope": sorted(ROLLBACK_STATUSES),
            "candidate_records": len(candidate_ids),
            "resettable_records": len(resettable),
            "skipped_current_status": dict(sorted(skipped_statuses.items())),
            "missing_records": len(missing_ids),
            "protected_records": len(protected),
            "resettable_sample": _sample(row.manga_id for row in resettable),
            "protected_sample": [
                {"manga_id": manga_id, "effects": effects}
                for manga_id, effects in sorted(protected.items())[:SAMPLE_LIMIT]
            ],
            "missing_sample": _sample(missing_ids),
        }
        if protected:
            report["result"] = "blocked"
            report["reason"] = "eligible_records_have_downstream_business_effects"
            return report
        if not apply:
            report["result"] = "would_requeue"
            return report

        now = utcnow()
        for row in resettable:
            previous = row.status
            row.status = Status.DEFERRED.value
            row.screen_pending = False
            row.screen_group_id = None
            row.defer_until = None
            row.remark = "collect_rollback"
            row.status_updated_at = row.updated_at = now
            row.row_version += 1
            session.add(
                EventLog(
                    manga_id=row.manga_id,
                    run_id=run_id,
                    component="rollback",
                    event_type="collect_rollback",
                    operation="collect",
                    from_status=previous,
                    to_status=Status.DEFERRED.value,
                    actor="rollback_operation",
                    detail={"reason": "requeue_for_collect"},
                )
            )
        session.add(
            EventLog(
                manga_id=None,
                run_id=run_id,
                component="rollback",
                event_type="collect_rollback_finished",
                operation="collect",
                actor="rollback_operation",
                detail={
                    "requeued": len(resettable),
                    "skipped_current_status": dict(skipped_statuses),
                    "missing": len(missing_ids),
                },
            )
        )
        session.flush()
        report["result"] = "requeued"
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rollback_operation")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--database-only",
        action="store_true",
        help="acknowledge that this command only changes PostgreSQL",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview only (default)")
    mode.add_argument("--apply", action="store_true", help="requeue the eligible records")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    app, _, _, _ = load_config(args.config_dir)
    result = rollback_collect(Database(app.database_url), args.run_id, apply=bool(args.apply))
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    print(payload)
    if args.report:
        args.report.write_text(payload + "\n", encoding="utf-8")
    return 0 if result.get("result") in {"would_requeue", "requeued"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
