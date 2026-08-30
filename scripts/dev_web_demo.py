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
        session.commit()


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
