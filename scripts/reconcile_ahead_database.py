"""Roll back database rows that are ahead of the target LANraragi inventory.

This migration helper supports replacing machine B's database with a newer
database copied from machine A.  Preview is the default.  Only rows in
``completed`` or ``uploaded`` whose numeric EH/EX gallery ID is absent from
machine B's LANraragi archive list are eligible for rollback.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from eh_archive.config import load_config
from eh_archive.db import Database
from eh_archive.db.models import DOWNLOAD_METHOD_VALUES, EventLog, MangaRecord
from eh_archive.services.uploader.lanraragi import LANraragiApiGateway
from eh_archive.special.modules.lanraragi_compare.comparison import (
    numeric_archive_id,
    numeric_database_id,
)

SOURCE_STATUSES = frozenset({"completed", "uploaded"})
ARTIFACT_FIELDS = (
    "artifact_location",
    "artifact_filename",
    "rename_target_filename",
    "artifact_kind",
    "artifact_size",
    "artifact_sha1",
    "artifact_checked_at",
)


def _read_archives(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("archive JSON must contain the list returned by /api/archives")
    return payload


def _write_report(log_dir: str | Path, payload: dict[str, Any]) -> Path:
    directory = Path(log_dir) / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    path = directory / f"ahead_database_reconcile_{stamp}.json"
    sequence = 2
    while path.exists():
        path = directory / f"ahead_database_reconcile_{stamp}_{sequence}.json"
        sequence += 1
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _inventory(archives: list[dict[str, Any]]) -> tuple[set[int], list[dict[str, str]]]:
    gallery_ids: set[int] = set()
    unparsed: list[dict[str, str]] = []
    for archive in archives:
        gallery_id = numeric_archive_id(archive)
        if gallery_id is None:
            unparsed.append(
                {
                    "arcid": str(archive.get("arcid", "")),
                    "title": str(archive.get("title", "")),
                }
            )
        else:
            gallery_ids.add(gallery_id)
    return gallery_ids, unparsed


def _plan_row(row: MangaRecord) -> dict[str, Any]:
    active = bool(row.active_attempt_id or row.lease_token or row.lease_owner or row.lease_until)
    target_status = (
        "download_pending" if row.download_method in DOWNLOAD_METHOD_VALUES else "discovered"
    )
    return {
        "manga_id": row.manga_id,
        "name": row.name or row.real_name,
        "from_status": row.status,
        "to_status": target_status,
        "download_method": row.download_method,
        "lrr_archive_id": row.lrr_archive_id,
        "artifact_filename": row.artifact_filename,
        "artifact_generation": row.artifact_generation,
        "active_execution": active,
    }


def _apply_row(session, row: MangaRecord, plan: dict[str, Any], now: datetime) -> None:
    before = {
        "lrr_archive_id": row.lrr_archive_id,
        "external_download_id": row.external_download_id,
        "artifact_location": row.artifact_location,
        "artifact_filename": row.artifact_filename,
        "artifact_generation": row.artifact_generation,
        "artifact_size": row.artifact_size,
        "artifact_sha1": row.artifact_sha1,
    }
    previous_status = row.status
    row.status = plan["to_status"]
    row.queue_source = "manual"
    row.defer_until = None
    row.next_retry_at = None
    row.external_download_id = None
    row.lrr_archive_id = None
    for field in ARTIFACT_FIELDS:
        setattr(row, field, None)
    # Keep artifact_generation monotonic.  A future download will register the
    # next generation, preventing reuse of a generation from the copied DB.
    row.last_error_operation = None
    row.last_error_code = None
    row.last_error_detail = None
    row.last_error_at = None
    if row.status == "discovered":
        row.screen_group_id = None
    row.status_updated_at = now
    row.updated_at = now
    row.row_version += 1
    session.add(
        EventLog(
            manga_id=row.manga_id,
            component="migration_reconcile",
            event_type="status_override",
            operation="rollback_missing_archive",
            from_status=previous_status,
            to_status=row.status,
            actor="local_script",
            detail={
                "reason": "archive is absent from target LANraragi inventory",
                "inventory_authority": "target_lanraragi",
                "cleared": before,
            },
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or roll back completed/uploaded database rows missing from "
            "the target LANraragi inventory."
        )
    )
    parser.add_argument("--config-dir", default="config")
    parser.add_argument(
        "--archives",
        type=Path,
        help="use a saved /api/archives JSON response instead of querying LANraragi",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--apply", action="store_true", help="apply the displayed rollback plan")
    parser.add_argument(
        "--expected-missing",
        type=int,
        help="required with --apply; must equal the preview's missing row count",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.apply and args.expected_missing is None:
        parser.error("--apply requires --expected-missing from a fresh preview")
    if args.expected_missing is not None and args.expected_missing < 0:
        parser.error("--expected-missing cannot be negative")

    app, _, _, secrets = load_config(args.config_dir)
    if args.archives:
        archives = _read_archives(args.archives)
        inventory_source = str(args.archives.resolve())
    else:
        gateway = LANraragiApiGateway(
            app.lanraragi_url,
            headers=secrets.lanraragi,
            timeout=args.timeout,
        )
        try:
            archives = gateway.list_archives()
        finally:
            if gateway.session is not None:
                gateway.session.close()
        inventory_source = f"{app.lanraragi_url.rstrip('/')}/api/archives"

    archive_ids, unparsed_archives = _inventory(archives)
    database = Database(app.database_url)
    applied = False
    try:
        with database.session() as session:
            rows = list(
                session.scalars(
                    select(MangaRecord)
                    .where(MangaRecord.status.in_(SOURCE_STATUSES))
                    .order_by(MangaRecord.manga_id)
                )
            )
            invalid_database_ids = [
                row.manga_id for row in rows if numeric_database_id(row.manga_id) is None
            ]
            missing_rows = [
                row
                for row in rows
                if (gallery_id := numeric_database_id(row.manga_id)) is not None
                and gallery_id not in archive_ids
            ]
            plans = [_plan_row(row) for row in missing_rows]
            active_plans = [plan for plan in plans if plan["active_execution"]]

            if args.apply:
                if args.expected_missing != len(plans):
                    raise RuntimeError(
                        "inventory changed: expected "
                        f"{args.expected_missing} missing rows, found {len(plans)}; preview again"
                    )
                if active_plans:
                    ids = ", ".join(plan["manga_id"] for plan in active_plans[:10])
                    raise RuntimeError(
                        f"refusing to modify {len(active_plans)} rows with active execution: {ids}"
                    )
                now = datetime.now(UTC)
                for row, plan in zip(missing_rows, plans, strict=True):
                    _apply_row(session, row, plan, now)
                applied = True

        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": "apply" if applied else "preview",
            "inventory_source": inventory_source,
            "inventory_archives": len(archives),
            "inventory_gallery_ids": len(archive_ids),
            "database_source_statuses": sorted(SOURCE_STATUSES),
            "database_source_rows": len(rows),
            "missing_rows": len(plans),
            "active_rows_refused": len(active_plans),
            "invalid_database_ids": invalid_database_ids,
            "unparsed_lanraragi_archives": unparsed_archives,
            "plan": plans,
        }
        report_path = _write_report(app.log_dir, report)
    finally:
        database.dispose()

    print(f"Mode: {report['mode']}")
    print(f"Database completed/uploaded rows: {report['database_source_rows']}")
    print(f"LANraragi gallery IDs: {report['inventory_gallery_ids']}")
    print(f"Missing rows: {report['missing_rows']}")
    print(f"Rows with active execution: {report['active_rows_refused']}")
    print(f"Unparsed LANraragi archives: {len(unparsed_archives)}")
    print(f"Report written: {report_path.resolve()}")
    if not applied:
        print(
            "Preview only. After reviewing the report, rerun with "
            f"--apply --expected-missing {len(plans)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
