"""Run the EH Archive web UI with a throwaway SQLite database and fake data.

Usage (from repo root):
    python scripts/dev_web_demo.py

Then open http://127.0.0.1:8787

Nothing is written to the project directory; the SQLite file lives in /tmp.
Set EHARCHIVE_WEB_DEMO_DB to reuse a persistent file instead.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

_persistent_demo_db = os.getenv("EHARCHIVE_WEB_DEMO_DB")
_db_path = (
    Path(_persistent_demo_db).expanduser().resolve()
    if _persistent_demo_db
    else Path(tempfile.gettempdir()) / f"eharchive_web_demo_{os.getpid()}.db"
)
# Never inherit EHARCHIVE_DATABASE_URL here: a development demo must not be
# able to connect to the configured production database by accident.
os.environ["EHARCHIVE_DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ.setdefault("EHARCHIVE_WEB_HOST", "127.0.0.1")
os.environ.setdefault("EHARCHIVE_WEB_PORT", "8787")
# No EHARCHIVE_WEB_SECRET / PASSWORD_HASH -> auth disabled, localhost-only mode.

from eh_archive.db.models import (
    Base,
    EventLog,
    JobAttempt,
    MangaInfoRecord,
    MangaRecord,
    SpecialJob,
    SpecialWorkflow,
    SystemControl,
    SystemHealth,
)
from eh_archive.db.session import Database
from eh_archive.web.app import create_app


def _now() -> datetime:
    return datetime.now(UTC)


def _create_tables_sqlite(database: Database) -> None:
    """Create all ORM tables, dropping the one Postgres-only regex CHECK."""
    from sqlalchemy import CheckConstraint

    manga = Base.metadata.tables["manga"]
    pg_only = [
        constraint
        for constraint in manga.constraints
        if isinstance(constraint, CheckConstraint) and "~" in str(constraint.sqltext)
    ]
    for constraint in pg_only:
        manga.constraints.remove(constraint)

    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateTable

    with database.engine.begin() as conn:
        for table in Base.metadata.tables.values():
            ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
            # SQLite autoincrement requires INTEGER PRIMARY KEY, not BIGINT.
            ddl = ddl.replace("BIGINT NOT NULL", "INTEGER NOT NULL")
            ddl = ddl.replace("BIGINT,", "INTEGER,")
            ddl = ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
            conn.exec_driver_sql(ddl)


def _seed(database: Database) -> None:
    _create_tables_sqlite(database)
    with database.session() as session:
        if session.query(MangaRecord).count():
            return

        now = _now()
        statuses = [
            ("download_pending", "manual", "direct", None, None),
            ("downloading", "automatic", "torrent", None, now + timedelta(minutes=5)),
            ("downloaded", "automatic", "direct", None, None),
            ("upload_pending", "automatic", "direct", None, None),
            ("uploaded", "automatic", "hah", None, None),
            ("completed", "manual", "direct", None, None),
            ("manual_review", "automatic", "direct", "LRR_409", now - timedelta(hours=2)),
            ("manual_review", "automatic", "torrent", "video_torrent", now),
            ("quarantined", "automatic", "direct", "CHECKSUM_MISMATCH", now - timedelta(days=1)),
            ("deferred", "manual", "aria2", None, now + timedelta(days=1)),
        ]
        for idx, (status, source, method, err_code, retry_at) in enumerate(statuses, start=1):
            manga_id = f"12345{idx:02d}/abcdef{idx:02d}"
            record = MangaRecord(
                manga_id=manga_id,
                name=f"[Sample Gallery {idx}] 示例画廊标题 {status}",
                real_name=f"sample_gallery_{idx}.zip",
                link=f"https://exhentai.org/g/{manga_id}/",
                posted_at=now - timedelta(days=idx),
                category="Doujinshi",
                tags_raw="language:chinese, parody:original",
                pages=20 + idx * 5,
                rating=4 + (idx % 2),
                uploader="demo_uploader",
                remark="演示数据" if idx % 3 == 0 else None,
                queue_source=source,
                status=status,
                priority=100 + idx * 10,
                download_method=method,
                next_retry_at=retry_at,
                lease_owner=f"worker-{idx}" if status == "downloading" else None,
                lease_until=now + timedelta(minutes=15) if status == "downloading" else None,
                active_attempt_id=idx if status == "downloading" else None,
                last_error_code=err_code,
                last_error_detail='{"error": "演示错误详情"}' if err_code else None,
                last_error_at=retry_at,
                artifact_location="direct_download",
                artifact_filename=f"sample_{idx}.zip",
                artifact_kind="zip",
                artifact_size=50_000_000 + idx * 10_000_000,
                artifact_sha1="a" * 40,
                lrr_archive_id="b" * 40 if status in {"uploaded", "completed"} else None,
                row_version=1,
                status_updated_at=now - timedelta(hours=idx),
                created_at=now - timedelta(days=idx + 1),
                updated_at=now - timedelta(hours=idx),
            )
            session.add(record)
            session.add(
                MangaInfoRecord(
                    manga_id=manga_id,
                    name=record.name,
                    roman_name=record.name,
                    real_name=record.real_name,
                    link=record.link,
                    category=record.category,
                    uploader=record.uploader,
                    posted_at=record.posted_at,
                )
            )
            session.add(
                JobAttempt(
                    id=idx,
                    manga_id=manga_id,
                    operation="direct_download" if method == "direct" else "torrent_download",
                    attempt_no=1,
                    status="running" if status == "downloading" else "succeeded",
                    trigger_source="supervisor",
                    actor="demo",
                    previous_status="download_pending",
                    resulting_status=status,
                    lease_token=f"demo-lease-{idx}",
                    started_at=now - timedelta(minutes=30),
                    finished_at=None if status == "downloading" else now - timedelta(minutes=25),
                    progress_bytes=30_000_000 if status == "downloading" else None,
                    progress_total_bytes=80_000_000 if status == "downloading" else None,
                    progress_speed_bps=1_500_000 if status == "downloading" else None,
                    progress_updated_at=now if status == "downloading" else None,
                )
            )
            session.add(
                EventLog(
                    manga_id=manga_id,
                    component="web",
                    event_type="status",
                    operation="demo",
                    actor="demo",
                    from_status="download_pending",
                    to_status=status,
                    error_code=err_code,
                    detail={"demo": True},
                    created_at=now - timedelta(hours=idx),
                )
            )

        for component, state, reason in [
            ("supervisor", "running", None),
            ("collector", "running", None),
            ("downloader", "paused", "演示暂停原因"),
            ("uploader", "running", None),
        ]:
            session.add(
                SystemControl(
                    component=component,
                    state=state,
                    reason=reason,
                    heartbeat_at=now - timedelta(seconds=30),
                    lease_owner=f"demo-worker-{component}" if state == "running" else None,
                    lease_until=now + timedelta(minutes=10) if state == "running" else None,
                    row_version=1,
                )
            )
        for component, status, latency in [
            ("eh", "healthy", 120),
            ("lanraragi", "healthy", 45),
            ("qbittorrent", "degraded", 800),
        ]:
            session.add(
                SystemHealth(
                    component=component,
                    status=status,
                    checked_at=now - timedelta(seconds=10),
                    latency_ms=latency,
                    message="演示健康检查",
                )
            )

        _seed_special(session, now)
        session.commit()


def _seed_special(session, now: datetime) -> None:
    """Seed special-workflow demo data covering every UI state."""

    torrent_choices = [
        {
            "choice_id": "ch_img_720",
            "label": "[Demo] 720p archive",
            "suggested_role": "image",
            "size": "1.2 GiB",
            "seeds": 8,
            "posted_at": "2026-08-01",
            "warnings": [],
        },
        {
            "choice_id": "ch_img_1080",
            "label": "[Demo] 1080p archive",
            "suggested_role": "image",
            "size": "2.4 GiB",
            "seeds": 3,
            "posted_at": "2026-07-15",
            "warnings": ["outdated"],
        },
        {
            "choice_id": "ch_vid_720",
            "label": "[Demo] 720p video",
            "suggested_role": "video",
            "size": "800 MiB",
            "seeds": 12,
            "posted_at": "2026-08-01",
            "warnings": [],
        },
        {
            "choice_id": "ch_vid_1080",
            "label": "[Demo] 1080p video",
            "suggested_role": "video",
            "size": "1.6 GiB",
            "seeds": 0,
            "posted_at": "2026-07-15",
            "warnings": ["no_seeders", "resampled"],
        },
    ]

    def _torrent(role: str, hash_suffix: str, progress: float, *, complete: bool = False) -> dict:
        state = "completed" if complete else "downloading"
        total = 1_500_000_000
        return {
            "role": role,
            "provider": "qbittorrent",
            "external_id": f"{'a' * 20}{hash_suffix}",
            "status": state,
            "qbit_state": "pausedUP" if complete else "downloading",
            "progress": 1.0 if complete else progress,
            "total_size": total,
            "downloaded_bytes": int(total * (1.0 if complete else progress)),
            "speed_bps": 0 if complete else 2_400_000,
            "eta_seconds": 0 if complete else 300,
            "completion_time": int(now.timestamp()) if complete else None,
            "content_path": f"/tmp/demo/{role}" if complete else None,
            "updated_at": now.isoformat(),
        }

    # Manga records dedicated to special-workflow scenarios (idx 11-16).
    special_manga = [
        # (idx, manga_status, err_code, remark, lease, attempt)
        (11, "special_processing", None, None, False, False),
        (12, "special_processing", None, None, False, False),
        (13, "special_processing", None, None, False, False),
        (14, "completed", None, None, False, False),
        (15, "special_processing", "ffmpeg_failed", None, False, False),
        (16, "manual_review", "video_torrent", None, False, False),
    ]
    for idx, m_status, err_code, remark, has_lease, has_attempt in special_manga:
        manga_id = f"12345{idx:02d}/abcdef{idx:02d}"
        session.add(MangaRecord(
            manga_id=manga_id,
            name=f"[Special Demo {idx}] 特殊处理演示画廊 {idx}",
            real_name=f"special_demo_{idx}.zip",
            link=f"https://exhentai.org/g/{manga_id}/",
            posted_at=now - timedelta(days=idx),
            category="Doujinshi",
            tags_raw="language:chinese, parody:original",
            pages=30 + idx * 3,
            rating=4,
            uploader="demo_uploader",
            remark=remark,
            queue_source="manual",
            status=m_status,
            priority=100 + idx * 10,
            download_method="torrent",
            last_error_code=err_code,
            last_error_detail=f'{{"error": "special demo error {err_code}"}}' if err_code else None,
            last_error_at=now - timedelta(hours=1) if err_code else None,
            artifact_location="prepared" if m_status == "completed" else None,
            artifact_filename=f"special_demo_{idx}.zip" if m_status == "completed" else None,
            artifact_kind="zip" if m_status == "completed" else None,
            artifact_size=80_000_000 + idx * 5_000_000 if m_status == "completed" else None,
            artifact_sha1="b" * 40 if m_status == "completed" else None,
            row_version=3,
            status_updated_at=now - timedelta(hours=idx),
            created_at=now - timedelta(days=idx + 2),
            updated_at=now - timedelta(hours=idx),
        ))
        session.add(MangaInfoRecord(
            manga_id=manga_id,
            name=f"[Special Demo {idx}] 特殊处理演示画廊 {idx}",
            real_name=f"special_demo_{idx}.zip",
            link=f"https://exhentai.org/g/{manga_id}/",
            category="Doujinshi",
            uploader="demo_uploader",
            posted_at=now - timedelta(days=idx),
        ))

    entry_payload = {
        "reason": "video_torrent_detected",
        "source_error_code": "video_torrent",
        "source_error_operation": "download",
        "source_error_detail": "gallery contains video torrent links",
    }

    # WF-1: awaiting_torrent_selection — user needs to pick image + video.
    wf1 = SpecialWorkflow(
        manga_id="1234511/abcdef11", kind="video_archive",
        status="active", phase="awaiting_torrent_selection",
        resume_status="manual_review",
        payload={
            "entry": entry_payload,
            "config_snapshot": {},
            "torrent_snapshot": {"fetched_at": now.isoformat(), "choices": torrent_choices},
            "selection": None,
            "torrents": [],
            "final_artifact": None,
        },
        progress={"message": "awaiting_torrent_selection", "total": 4},
        row_version=2,
        created_by="demo_admin",
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(minutes=20),
    )
    session.add(wf1)
    session.flush()
    session.add(SpecialJob(
        workflow_id=wf1.id, operation="load_torrent_options",
        status="succeeded", trigger_source="web", requested_by="demo_admin",
        attempt_no=1, next_run_at=now - timedelta(hours=3),
        started_at=now - timedelta(hours=3), finished_at=now - timedelta(hours=3) + timedelta(seconds=8),
        progress={"message": "awaiting_torrent_selection", "total": 4},
    ))
    session.add(EventLog(
        manga_id="1234511/abcdef11", component="special_processing",
        event_type="special_start", actor="demo_admin",
        from_status="manual_review", to_status="special_processing",
        detail={"workflow_id": wf1.id, "load_options": True, "entry_reason": "video_torrent_detected"},
        created_at=now - timedelta(hours=3),
    ))
    session.add(EventLog(
        manga_id="1234511/abcdef11", component="special_processing",
        event_type="special_job_succeeded", operation="load_torrent_options",
        actor="supervisor-demo",
        detail={"workflow_id": wf1.id, "choice_count": 4},
        created_at=now - timedelta(hours=3) + timedelta(seconds=8),
    ))

    # WF-2: downloading — both torrents in progress.
    wf2 = SpecialWorkflow(
        manga_id="1234512/abcdef12", kind="video_archive",
        status="active", phase="downloading",
        resume_status="manual_review",
        payload={
            "entry": entry_payload,
            "config_snapshot": {},
            "torrent_snapshot": {"fetched_at": now.isoformat(), "choices": torrent_choices},
            "selection": {
                "image_choice_id": "ch_img_720",
                "video_choice_id": "ch_vid_720",
                "confirmed_warnings": [],
                "selected_at": (now - timedelta(hours=2)).isoformat(),
                "selected_by": "demo_admin",
            },
            "torrents": [_torrent("image", "img01", 0.65), _torrent("video", "vid02", 0.42)],
            "final_artifact": None,
        },
        progress={"message": "downloading", "submitted": 2, "total": 2},
        row_version=5,
        created_by="demo_admin",
        created_at=now - timedelta(hours=6),
        updated_at=now - timedelta(minutes=5),
    )
    session.add(wf2)
    session.flush()
    for op, started_offset, finished_offset in [
        ("load_torrent_options", timedelta(hours=6), timedelta(hours=6, seconds=6)),
        ("submit_selected_torrents", timedelta(hours=2), timedelta(hours=2, seconds=15)),
    ]:
        session.add(SpecialJob(
            workflow_id=wf2.id, operation=op,
            status="succeeded", trigger_source="web", requested_by="demo_admin",
            attempt_no=1, next_run_at=now - started_offset,
            started_at=now - started_offset, finished_at=now - finished_offset,
            progress={},
        ))

    # WF-3: downloading with a queued check job (shows active job banner in UI).
    wf3 = SpecialWorkflow(
        manga_id="1234513/abcdef13", kind="video_archive",
        status="active", phase="checking_downloads",
        resume_status="manual_review",
        payload={
            "entry": entry_payload,
            "config_snapshot": {},
            "torrent_snapshot": {"fetched_at": now.isoformat(), "choices": torrent_choices},
            "selection": {
                "image_choice_id": "ch_img_1080",
                "video_choice_id": "ch_vid_720",
                "confirmed_warnings": ["image:outdated"],
                "selected_at": (now - timedelta(hours=1)).isoformat(),
                "selected_by": "demo_admin",
            },
            "torrents": [_torrent("image", "img03", 0.98), _torrent("video", "vid04", 0.87)],
            "final_artifact": None,
        },
        progress={"message": "checking_downloads", "completed": 0, "total": 2},
        row_version=7,
        created_by="demo_admin",
        created_at=now - timedelta(hours=8),
        updated_at=now - timedelta(minutes=1),
    )
    session.add(wf3)
    session.flush()
    session.add(SpecialJob(
        workflow_id=wf3.id, operation="check_and_compose_if_ready",
        status="running", trigger_source="web", requested_by="demo_admin",
        attempt_no=1, next_run_at=now - timedelta(minutes=2),
        lease_token="demo-lease-check-01", lease_owner="supervisor-demo",
        lease_until=now + timedelta(hours=20),
        started_at=now - timedelta(minutes=2),
        progress={"message": "checking_downloads"},
    ))

    # WF-4: completed + ready, source_cleanup pending (manga already completed).
    wf4 = SpecialWorkflow(
        manga_id="1234514/abcdef14", kind="video_archive",
        status="completed", phase="ready",
        resume_status="manual_review",
        payload={
            "entry": entry_payload,
            "config_snapshot": {},
            "torrent_snapshot": {"fetched_at": now.isoformat(), "choices": torrent_choices},
            "selection": {
                "image_choice_id": "ch_img_720",
                "video_choice_id": "ch_vid_720",
                "confirmed_warnings": [],
                "selected_at": (now - timedelta(days=2)).isoformat(),
                "selected_by": "demo_admin",
            },
            "torrents": [_torrent("image", "img05", 1.0, complete=True), _torrent("video", "vid06", 1.0, complete=True)],
            "final_artifact": {
                "location": "prepared",
                "filename": "special_demo_14.zip",
                "kind": "zip",
                "generation": 1,
                "size": 85_000_000,
                "sha1": "c" * 40,
                "checked_at": (now - timedelta(hours=20)).isoformat(),
            },
            "source_cleanup": {"status": "pending", "job_id": None, "last_error": None, "last_error_code": None},
            "workspace": {"workflow_id": None, "relative_path": "1234514/w4", "counts": {"pictures": 45, "webps": 3, "original_video_files": 3}},
        },
        progress={"message": "ready", "completed": 1, "total": 1},
        row_version=9,
        created_by="demo_admin",
        created_at=now - timedelta(days=3),
        updated_at=now - timedelta(hours=20),
        completed_at=now - timedelta(hours=20),
    )
    session.add(wf4)
    session.flush()
    for op in ("load_torrent_options", "submit_selected_torrents", "check_and_compose_if_ready"):
        session.add(SpecialJob(
            workflow_id=wf4.id, operation=op,
            status="succeeded", trigger_source="web", requested_by="demo_admin",
            attempt_no=1, next_run_at=now - timedelta(days=2),
            started_at=now - timedelta(days=2), finished_at=now - timedelta(days=2) + timedelta(minutes=30),
            progress={},
        ))

    # WF-5: failed — ffmpeg conversion blew up, retry_operation set.
    wf5 = SpecialWorkflow(
        manga_id="1234515/abcdef15", kind="video_archive",
        status="active", phase="failed",
        resume_status="manual_review",
        payload={
            "entry": entry_payload,
            "config_snapshot": {},
            "torrent_snapshot": {"fetched_at": now.isoformat(), "choices": torrent_choices},
            "selection": {
                "image_choice_id": "ch_img_720",
                "video_choice_id": "ch_vid_1080",
                "confirmed_warnings": ["video:no_seeders", "video:resampled"],
                "selected_at": (now - timedelta(hours=4)).isoformat(),
                "selected_by": "demo_admin",
            },
            "torrents": [_torrent("image", "img07", 1.0, complete=True), _torrent("video", "vid08", 1.0, complete=True)],
            "final_artifact": None,
            "retry_operation": "check_and_compose_if_ready",
        },
        progress={"message": "failed"},
        error_code="ffmpeg_failed",
        error_detail="ffmpeg failed for video_03.mp4: conversion timed out after 300s",
        row_version=8,
        created_by="demo_admin",
        created_at=now - timedelta(hours=10),
        updated_at=now - timedelta(minutes=30),
    )
    session.add(wf5)
    session.flush()
    for op, status in [
        ("load_torrent_options", "succeeded"),
        ("submit_selected_torrents", "succeeded"),
        ("check_and_compose_if_ready", "failed"),
    ]:
        session.add(SpecialJob(
            workflow_id=wf5.id, operation=op,
            status=status, trigger_source="web", requested_by="demo_admin",
            attempt_no=1, next_run_at=now - timedelta(hours=4),
            started_at=now - timedelta(hours=4),
            finished_at=now - timedelta(hours=4) + timedelta(minutes=45) if status == "failed" else now - timedelta(hours=4) + timedelta(seconds=10),
            progress={},
            error_code="ffmpeg_failed" if status == "failed" else None,
            error_detail="ffmpeg failed for video_03.mp4: conversion timed out after 300s" if status == "failed" else None,
        ))
    session.add(EventLog(
        manga_id="1234515/abcdef15", component="special_processing",
        event_type="special_job_failed", operation="check_and_compose_if_ready",
        actor="supervisor-demo", error_code="ffmpeg_failed",
        detail={"workflow_id": wf5.id, "summary": "ffmpeg conversion timed out"},
        created_at=now - timedelta(minutes=30),
    ))

    # WF-6: cancelled — manga restored to manual_review.
    wf6 = SpecialWorkflow(
        manga_id="1234516/abcdef16", kind="video_archive",
        status="cancelled", phase="cancelled",
        resume_status="manual_review",
        payload={
            "entry": entry_payload,
            "config_snapshot": {},
            "torrent_snapshot": {"fetched_at": now.isoformat(), "choices": torrent_choices},
            "selection": None,
            "torrents": [],
            "final_artifact": None,
        },
        progress={"message": "cancelled"},
        row_version=3,
        created_by="demo_admin",
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(hours=18),
        completed_at=now - timedelta(hours=18),
    )
    session.add(wf6)
    session.flush()
    for op, status in [
        ("load_torrent_options", "succeeded"),
        ("cancel_video_archive", "succeeded"),
    ]:
        session.add(SpecialJob(
            workflow_id=wf6.id, operation=op,
            status=status, trigger_source="web", requested_by="demo_admin",
            attempt_no=1, next_run_at=now - timedelta(hours=18),
            started_at=now - timedelta(hours=18),
            finished_at=now - timedelta(hours=18) + timedelta(seconds=5),
            progress={},
        ))
    session.add(EventLog(
        manga_id="1234516/abcdef16", component="special_processing",
        event_type="special_cancelled", operation="cancel_video_archive",
        actor="demo_admin", from_status="special_processing", to_status="manual_review",
        detail={"workflow_id": wf6.id, "torrent_cleanup": {"deleted": [], "skipped": []}},
        created_at=now - timedelta(hours=18),
    ))


def _demo_config_dir() -> Path:
    """Build a minimal config directory so load_config() works without real paths."""
    config_dir = Path(tempfile.gettempdir()) / "eharchive_web_demo_config"
    config_dir.mkdir(exist_ok=True)
    demo_root = Path(tempfile.gettempdir()) / "eharchive-web-demo"
    roots = {
        name: demo_root / name
        for name in (
            "direct_download",
            "torrent_download",
            "hah_download",
            "aria2_download",
            "prepared",
            "quarantine",
            "trash",
        )
    }
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    root_lines = "\n".join(f'{name} = "{root.as_posix()}"' for name, root in roots.items())
    (config_dir / "app.toml").write_text(
        f"""\
database_url = "sqlite:///{_db_path.as_posix()}"
web_host = "127.0.0.1"
web_port = 8787

[roots]
{root_lines}
""",
        encoding="utf-8",
    )
    for name in ("supervisor.toml", "crawl.toml", "secrets.toml"):
        target = config_dir / name
        if not target.exists():
            target.write_text("", encoding="utf-8")
    special_dir = config_dir / "special"
    special_dir.mkdir(exist_ok=True)
    module_root = Path(tempfile.gettempdir()) / "eharchive-web-demo-video"
    workspace_root = module_root / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    module_config = special_dir / "video_archive.toml"
    module_config.write_text(
        "enabled = true\nauto_start = false\n"
        '[download]\ncategory = "eharchive-demo-video"\n'
        f'[work]\nworkspace_root = "{workspace_root.as_posix()}"\nmax_concurrency = 1\n'
        f'[ffmpeg]\nexecutable = "{(module_root / "ffmpeg-placeholder").as_posix()}"\n'
        "max_workers = 1\nquality = 75\ncompression_level = 6\n"
        "[output]\ninclude_original_mp4 = false\nlayout = \"legacy_folders\"\n",
        encoding="utf-8",
    )
    return config_dir


def main() -> None:
    database = Database(os.environ["EHARCHIVE_DATABASE_URL"])
    _seed(database)
    app = create_app(database, config_dir=_demo_config_dir())
    import uvicorn

    print(f"Demo database: {_db_path}")
    print("Open http://127.0.0.1:8787  (auth disabled in demo mode)")
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")


if __name__ == "__main__":
    main()
