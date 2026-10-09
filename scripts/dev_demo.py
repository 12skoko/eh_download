"""Run the EH Archive web UI against a throwaway SQLite database with fake data.

Usage (from repo root):
    python scripts/dev_demo.py [--port PORT] [--base DIR]
Then open the printed URL: /uiv3/ (also /uiv2/ and the original pages at /).
Auth is disabled and the server only listens on loopback.

Everything lives in a temporary directory (or ``--base``); configured databases
are never used. The management page (/system) shows fake systemd/Git status and
a sample operation; submitting operations (update / start / stop / restart) is
refused, while "check for updates" still runs ``git fetch`` on this repository.

``build_demo()`` is also used by the uiv3 test suite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Never inherit the configured database: load_config() prefers this variable over
# the demo's own config files, so a demo started from a production shell could
# otherwise point at the real database. Each demo's config names its own SQLite file.
os.environ.pop("EHARCHIVE_DATABASE_URL", None)

from eh_archive.db.models import (
    Base,
    EventLog,
    JobAttempt,
    MangaInfoRecord,
    MangaRecord,
    SpecialJob,
    SpecialWorkflow,
    SpecialWorkflowManga,
    SystemControl,
    SystemHealth,
)
from eh_archive.db.session import Database

OWNER = "supervisor@demo-nas:4242"
TITLES = [
    "[Circle Yomogi] 夏の終わりに 総集編",
    "[Hanabira] 花火の夜 [中国翻訳]",
    "[Kitsune Works] 月下の約束 ～完全版～",
    "[Studio Pastel] Seaside Memories",
    "[Moonlit Atelier] 星降る街のアトリエ",
    "[Kuro Neko] Autumn Leaves Anthology",
    "[Sora-iro] 空色デイズ 総集編",
    "[Pixel Garden] Weekend Sketches Vol.2",
    "(C104) [Mizuiro] 青の記憶",
    "[Aoi Sora] 夏の日の君へ",
    "[Kumo] 夏の雲 [DL版]",
    "[Hoshi] 星座のしずく",
    "[Natsuiro] 夏色レシピ",
    "[Yuki Koubou] 雪解けの頃",
    "[Akane] 茜色の放課後",
    "[Shiro Usagi] 白兎の手紙",
    "[Midori] 翠の森で",
    "[Rin] りんごの季節",
    "[Tsubaki] 椿の咲く家",
    "[Kaede] 楓並木の約束",
]


def _now() -> datetime:
    return datetime.now(UTC)


def _token(value: int) -> str:
    return hashlib.sha1(str(value).encode()).hexdigest()[:10]


def create_tables(database: Database) -> None:
    """Create ORM tables on SQLite, skipping PostgreSQL-only CHECK constraints."""
    from sqlalchemy import CheckConstraint
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateTable

    for table in Base.metadata.tables.values():
        for constraint in list(table.constraints):
            if isinstance(constraint, CheckConstraint) and "~" in str(constraint.sqltext):
                table.constraints.remove(constraint)
    with database.engine.begin() as conn:
        for table in Base.metadata.tables.values():  # SQLite does not enforce FK order here
            ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
            ddl = ddl.replace("BIGINT NOT NULL", "INTEGER NOT NULL").replace("BIGINT,", "INTEGER,")
            conn.exec_driver_sql(ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))


DEMO_PASSWORD = "demo-password"
DEMO_SECRET = "demo-secret-for-tests-0123456789"


def make_config_dir(base: Path, db_url: str, *, auth: bool = False) -> Path:
    config_dir = base / "config"
    (config_dir / "special").mkdir(parents=True, exist_ok=True)
    data = base / "data"
    roots = {
        name: data / name
        for name in (
            "torrent_download", "hah_download", "direct_download", "aria2_download",
            "prepared", "quarantine", "trash",
        )
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    root_lines = "\n".join(f'{k} = "{v.as_posix()}"' for k, v in roots.items())
    (config_dir / "app.toml").write_text(
        f'config_version = 2\ndatabase_url = "{db_url}"\ntimezone = "Asia/Shanghai"\n'
        f'log_dir = "{(base / "logs").as_posix()}"\nweb_host = "127.0.0.1"\nweb_port = 8787\n'
        f'\n[sessions.full_collect]\naccount = "browse"\nnetwork = "pool"\n'
        f'\n[roots]\n{root_lines}\n',
        encoding="utf-8",
    )
    sample = ROOT / "config.sample"
    for name in ("supervisor.toml", "crawl.toml"):
        shutil.copy(sample / name, config_dir / name)
    header = "config_version = 1\n"
    if auth:
        from eh_archive.web.auth import hash_password

        header += (
            f'web_username = "admin"\nweb_secret = "{DEMO_SECRET}"\n'
            f'web_password_hash = "{hash_password(DEMO_PASSWORD)}"\n'
        )
    secrets = header + (
        '[accounts.default]\ncookies_str = "ipb_member_id=1;ipb_pass_hash=demo"\n'
        '[accounts.browse]\ncookies_str = "ipb_member_id=2;ipb_pass_hash=demo"\n'
        '[accounts.archive]\ncookies_str = "ipb_member_id=3;ipb_pass_hash=demo"\n'
        '[networks.direct]\n'
        '[networks.pool]\nproxy_pool = ["hk1", "hk2"]\n'
        '[networks.hk1]\nproxies = { https = "http://127.0.0.1:18001" }\n'
        '[networks.hk2]\nproxies = { https = "http://127.0.0.1:18002" }\n'
    )
    (config_dir / "secrets.toml").write_text(secrets, encoding="utf-8")
    for name in (
        "download_cleanup.toml", "lanraragi_compare.toml", "lanraragi_metadata.toml",
        "manual_torrent.toml", "full_collect.toml",
    ):
        shutil.copy(sample / "special" / name, config_dir / "special" / name)
    full = (config_dir / "special" / "full_collect.toml").read_text(encoding="utf-8")
    full = full.replace("enabled = false", "enabled = true", 1)
    full = full.replace('base_url = ""', 'base_url = "https://exhentai.org/"', 1)
    (config_dir / "special" / "full_collect.toml").write_text(full, encoding="utf-8")
    work = base / "video-work"
    work.mkdir(exist_ok=True)
    video = (sample / "special" / "video_archive.toml").read_text(encoding="utf-8")
    video = video.replace("D:/eharchive-data/special-video-work", work.as_posix())
    video = video.replace("D:/ffmpeg/bin/ffmpeg.exe", (base / "ffmpeg-placeholder").as_posix())
    (config_dir / "special" / "video_archive.toml").write_text(video, encoding="utf-8")
    return config_dir


def _write_logs(log_dir: Path, full_collect_id: int, now: datetime) -> None:
    (log_dir / "special" / "full_collect").mkdir(parents=True, exist_ok=True)
    (log_dir / "tools").mkdir(parents=True, exist_ok=True)
    lines = [
        f"{(now - timedelta(minutes=60 - i)).isoformat()} INFO worker[{i % 3}] 处理批次 {i}，耗时 {i * 0.7:.1f}s"
        for i in range(60)
    ]
    (log_dir / "web.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (log_dir / "supervisor.log").write_text(
        "\n".join(line.replace("worker", "supervisor") for line in lines) + "\n", encoding="utf-8"
    )
    records = [
        {"time": (now - timedelta(minutes=30 - i)).isoformat(), "level": level, "message": message}
        for i, (level, message) in enumerate(
            [("INFO", "开始第 12 页"), ("INFO", "页面提交完成，新增 18 条"), ("WARNING", "代理切换：timeout"),
             ("INFO", "开始第 13 页"), ("ERROR", "E-H 返回 509，进入冷却"), ("INFO", "收到暂停意图，保存检查点")]
        )
    ]
    name = f"full_collect_workflow-{full_collect_id}_job-3_20260930.jsonl"
    (log_dir / "special" / "full_collect" / name).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )


def _output(log_dir: Path, workflow_id: int, job_id: int, output_id: str, data: dict, now) -> dict:
    key = f"{workflow_id}/{job_id}/demo-lease-{job_id}/{output_id}.json"
    path = log_dir / "special_outputs" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(raw, encoding="utf-8")
    return {
        "id": output_id, "job_id": job_id, "name": f"{output_id}.json",
        "media_type": "application/json", "size_bytes": len(raw.encode()),
        "storage_key": key, "created_at": now.isoformat(),
    }


def seed(database: Database, log_dir: Path, now: datetime | None = None) -> None:
    from eh_archive.special.catalog import load_modules
    from eh_archive.special.core.registry import get_workflow_definition

    load_modules()
    now = now or _now()
    data_root = log_dir.parent / "data"
    counter = iter(range(1, 10_000))

    with database.session() as session:
        if session.query(MangaRecord).count():
            return

        def manga(status, *, title=None, method=None, code=None, op=None, detail=None,
                  age_hours=None, **extra):
            idx = next(counter)
            manga_id = extra.pop("manga_id", None) or f"{3018000 - idx * 37}/{_token(idx)}"
            title = title or TITLES[idx % len(TITLES)] + (f" 第{idx}話" if idx >= len(TITLES) else "")
            age = timedelta(hours=age_hours if age_hours is not None else idx * 1.7)
            row = MangaRecord(
                manga_id=manga_id, name=title,
                real_name=title.replace("[", "[Romaji ").replace("の", " no "),
                link=f"https://exhentai.org/g/{manga_id}/", posted_at=now - age - timedelta(days=2),
                category="Doujinshi", uploader=f"uploader_{idx % 7}",
                tags_raw="language:chinese, parody:original, artist:demo, other:full color",
                pages=18 + idx % 90, rating=4, queue_source="manual" if idx % 5 == 0 else "automatic",
                status=status, priority=[0, 50, 100, 120][idx % 4], download_method=method,
                last_error_code=code, last_error_operation=op if code else None,
                last_error_detail=detail if code else None,
                last_error_at=now - age if code else None,
                row_version=3, status_updated_at=now - age,
                created_at=now - age - timedelta(days=1), updated_at=now - age,
            )
            for key, value in extra.items():
                setattr(row, key, value)
            session.add(row)
            session.add(MangaInfoRecord(
                manga_id=manga_id, name=title, roman_name=row.real_name, real_name=row.real_name,
                link=row.link, category=row.category, uploader=row.uploader, posted_at=row.posted_at,
                language="chinese", pages=row.pages, rating=row.rating, tags_raw=row.tags_raw,
            ))
            session.add(EventLog(
                manga_id=manga_id, component="collect", event_type="collected", operation="collect",
                actor="supervisor", from_status=None, to_status="discovered",
                created_at=now - age - timedelta(hours=3), detail={"page": 12},
            ))
            session.add(EventLog(
                manga_id=manga_id, component="supervisor", event_type="status", operation=op or "screen",
                actor="supervisor", from_status="discovered", to_status=status, error_code=code,
                detail={"error": detail} if detail else {"demo": True}, created_at=now - age,
            ))
            session.flush()
            return row

        def attempt(row, operation, status="succeeded", **extra):
            item = JobAttempt(
                manga_id=row.manga_id, operation=operation, attempt_no=1, status=status,
                trigger_source="supervisor", actor="supervisor", previous_status="download_pending",
                resulting_status=None if status == "running" else row.status,
                lease_token=(row.lease_token if status == "running" and row.lease_token
                             else f"lease-{row.manga_id}-{operation}"[:36]),
                started_at=now - timedelta(minutes=40),
                finished_at=None if status == "running" else now - timedelta(minutes=35),
                **extra,
            )
            session.add(item)
            session.flush()
            return item

        # --- 流水线各状态的普通数据 -------------------------------------------------
        fill = {
            "deferred": 5, "discovered": 3, "download_pending": 6, "downloading": 1,
            "download_blocked": 1, "downloaded": 2, "validating": 1, "preparing": 1,
            "upload_pending": 2, "uploading": 1, "uploaded": 2, "completed": 8,
            "quarantined": 2, "unavailable": 2, "cancel_requested": 1, "cancelled": 2,
            "outdated": 1, "force_delete_pending": 1, "deleted": 3, "filtered_out": 4, "skipped": 2,
        }
        errors = {
            "download_pending": ("torrent_no_seeders", "torrent_download", '{"error": "所有候选种子均无 Seeder"}'),
            "download_blocked": ("no_download_method", "torrent_download", "账号无下载额度"),
            "quarantined": ("checksum_mismatch", "validate", "ZIP CRC 校验失败：page_031.jpg"),
            "unavailable": ("gallery_unavailable", "details", "画廊已被移除"),
        }
        for status, count in fill.items():
            for index in range(count):
                code, op, detail = errors.get(status, (None, None, None))
                row = manga(
                    status, method="torrent" if index % 2 else "direct",
                    code=code if index == 0 else None, op=op, detail=detail,
                    artifact_location="direct_download" if status in {"downloaded", "upload_pending", "uploaded", "completed", "quarantined"} else None,
                    artifact_filename=f"archive_{index}_{status}.zip" if status in {"downloaded", "upload_pending", "uploaded", "completed", "quarantined"} else None,
                    artifact_kind="zip" if status in {"downloaded", "upload_pending", "uploaded", "completed"} else None,
                    artifact_size=80_000_000 + index * 3_000_000,
                    artifact_sha1=hashlib.sha1(f"{status}{index}".encode()).hexdigest(),
                    lrr_archive_id=hashlib.sha1(f"lrr{status}{index}".encode()).hexdigest() if status in {"uploaded", "completed"} else None,
                    next_retry_at=now + timedelta(minutes=12) if status == "download_pending" and index < 2 else None,
                )
                attempt(row, "details")

        # --- 场景：直接下载进行中（进度 + 取消） ------------------------------------------
        live = manga("downloading", title="[Circle Yomogi] 夏の終わりに 総集編 +α", method="direct",
                     lease_owner=OWNER, lease_until=now + timedelta(minutes=14),
                     lease_token="demo-direct-lease", age_hours=0.2)
        live_attempt = attempt(live, "direct_download", "running", progress_bytes=49_600_000,
                               progress_total_bytes=80_000_000, progress_speed_bps=1_450_000,
                               progress_updated_at=now)
        live.active_attempt_id = live_attempt.id

        # --- 场景：上传任务租约已过期 ---------------------------------------------------
        stale = manga("uploading", title="[Hiroki] Morning Glory Vol.3", method="torrent",
                      artifact_location="prepared", artifact_filename="morning_glory_3.zip",
                      artifact_kind="zip", artifact_size=233_000_000, artifact_sha1="d" * 40,
                      lease_owner="supervisor@old-host:99", lease_until=now - timedelta(minutes=50),
                      lease_token="demo-stale-lease", age_hours=1.5)
        stale_attempt = attempt(stale, "upload", "running")
        stale.active_attempt_id = stale_attempt.id

        # --- 场景：人工复核各类原因 ------------------------------------------------------
        manga("manual_review", title="[Kitsune Works] 月下の約束 ～完全版～", method="torrent",
              code="video_torrent", op="torrent_download",
              detail='{"error": "gallery contains video torrent links", "candidates": 3}', age_hours=0.2)
        manga("manual_review", title="(C104) [Mizuiro] 青の記憶 [DL版]", method="torrent",
              code="torrent_size_too_small", op="torrent_download",
              detail="种子 312 MB，小于预计 820 MB 的 60%", age_hours=0.9,
              torrent_review={"scope": {"candidate": "c1", "expected_size": 820_000_000},
                              "warnings": ["torrent_size_too_small"], "accepted_warnings": []})
        manga("manual_review", title="[Tsubaki] 椿の咲く家", method="torrent", code="invalid_torrent",
              op="torrent_download", detail="下载链接返回的内容不是有效的种子文件", age_hours=2)
        dup_file = "[Sample Gallery 7] 示例画廊标题.zip"
        (data_root / "direct_download").mkdir(parents=True, exist_ok=True)
        (data_root / "direct_download" / dup_file).write_bytes(b"PK\x05\x06" + b"\0" * 18)
        manga("manual_review", title="[Sample Gallery 7] 示例画廊标题 · 夏季增刊号", method="direct",
              code="lrr_duplicate", op="upload", detail="LANraragi 返回 409：已存在同名档案",
              artifact_location="direct_download", artifact_filename=dup_file, artifact_kind="zip",
              artifact_size=118_400_000, artifact_sha1="3fa1c09be2d44d7e8b0c6a55e1f2a7d913c0b8e4",
              age_hours=2.5)
        manga("completed", title="[Sample Gallery 7] 示例画廊标题", artifact_filename=dup_file,
              artifact_location="direct_download", lrr_archive_id="b1c2" + "0" * 32 + "9f0a")
        manga("uploaded", title="[Sample Gallery 7] 示例画廊标题 [改訂版]", artifact_filename=dup_file,
              artifact_location="direct_download", lrr_archive_id="e" * 40)
        manga("manual_review", title="[Akane] 茜色の放課後", method="direct", code="upload_result_unknown",
              op="upload", detail="<!doctype html><html><body>502 Bad Gateway</body></html>",
              artifact_location="prepared", artifact_filename="akane.zip", age_hours=3)
        manga("manual_review", title="[Midori] 翠の森で", method="torrent", code="lrr_metadata_mismatch",
              op="upload", detail="标题与标签回读不一致", artifact_location="prepared",
              artifact_filename="midori.zip", age_hours=4)
        manga("manual_review", title="[Rin] りんごの季節", method="direct", code="archive_unavailable",
              op="direct_download", detail="下载主机返回 410", age_hours=5)
        manga("manual_review", title="[Kaede] 楓並木の約束", method="direct",
              code="direct_download_cancelled", op="direct_download", detail="用户主动取消直接下载",
              age_hours=6)
        manga("rename_pending", title="[Shiro Usagi] 白兎の手紙", artifact_location="direct_download",
              artifact_filename="shiro.zip", rename_target_filename="[3001] shiro (2).zip")

        # --- 场景：上传等待旧档案删除 ----------------------------------------------------
        newer = manga("upload_pending", title="[Yuki Koubou] 雪解けの頃 [改訂版]",
                      artifact_location="prepared", artifact_filename="yuki_v2.zip")
        manga("outdated", title="[Yuki Koubou] 雪解けの頃", superseded_by_id=newer.manga_id,
              lrr_archive_id="f" * 40)

        # --- 场景：已完成且保存了种子告警授权 ------------------------------------------------
        manga("completed", title="[Natsuiro] 夏色レシピ", method="torrent", lrr_archive_id="a" * 40,
              artifact_location="torrent_download", artifact_filename="natsuiro.zip",
              torrent_review={"scope": {"candidate": "c9"}, "warnings": ["video_torrent"],
                              "accepted_warnings": ["video_torrent"]})

        # --- 调度与健康 ----------------------------------------------------------------
        from eh_archive.config.loader import SUPERVISOR_MODULES

        session.add(SystemControl(component="supervisor", state="running", lease_owner=OWNER,
                                  lease_until=now + timedelta(minutes=10),
                                  heartbeat_at=now - timedelta(seconds=4), row_version=5))
        for name in SUPERVISOR_MODULES:
            control = SystemControl(component=name, state="running", lease_owner=OWNER, row_version=2,
                                    schedule_updated_at=now, schedule_running=False)
            if name == "collect":
                control.next_run_at = now + timedelta(minutes=38)
            if name == "torrent_check":
                control.next_run_at = now + timedelta(minutes=1)
                control.cooldown_until = now + timedelta(minutes=17, seconds=32)
                control.cooldown_owner = OWNER
                control.cooldown_reason = "E-H 返回 509，暂停访问"
            if name == "direct_download":
                control.state, control.reason = "paused", "账号额度用尽，暂停直链"
            session.add(control)
        for component, status, latency, message in [
            ("lanraragi", "healthy", 45, "连接正常"),
            ("qbittorrent", "degraded", 812, "响应缓慢"),
            ("storage:prepared", "healthy", 3, "读写正常"),
            ("storage:torrent_download", "degraded", 4, "空间偏低"),
        ]:
            session.add(SystemHealth(component=component, status=status, latency_ms=latency,
                                     message=message, checked_at=now - timedelta(seconds=10), detail={}))
        session.flush()

        # --- 特殊工作流 ---------------------------------------------------------------
        def workflow(kind, status, phase, payload, *, manga_row=None, resume="manual_review",
                     jobs=(), context=None, schema_version=None, **extra):
            definition = get_workflow_definition(kind)
            item = SpecialWorkflow(
                schema_version=schema_version or definition.schema_version, kind=kind, status=status, phase=phase,
                payload=payload, progress=extra.pop("progress", {}), row_version=4,
                created_by="web:admin", created_at=now - timedelta(hours=5),
                updated_at=now - timedelta(minutes=extra.pop("updated_minutes", 20)),
                resource_claims=[], **extra,
            )
            session.add(item)
            session.flush()
            if manga_row is not None:
                session.add(SpecialWorkflowManga(workflow_id=item.id, manga_id=manga_row.manga_id,
                                                 resume_status=resume, context=context or {}))
            seen: dict[str, int] = {}
            for operation, job_status, *rest in jobs:
                options = rest[0] if rest else {}
                seen[operation] = seen.get(operation, 0) + 1
                session.add(SpecialJob(
                    workflow_id=item.id, operation=operation, status=job_status, trigger_source="web",
                    requested_by="web:admin", attempt_no=seen[operation],
                    next_run_at=now - timedelta(minutes=30),
                    started_at=None if job_status == "queued" else now - timedelta(minutes=30),
                    finished_at=None if job_status in {"queued", "running"} else now - timedelta(minutes=29),
                    progress={}, **options,
                ))
            session.flush()
            session.add(EventLog(
                manga_id=manga_row.manga_id if manga_row else None, component="special_processing",
                event_type="special_start", operation=kind, actor="web:admin",
                detail={"workflow_id": item.id}, created_at=now - timedelta(hours=5),
            ))
            return item

        entry = {"reason": "video_torrent_detected", "source_error_code": "video_torrent"}
        video_context = {"entry": {"last_error_operation": "torrent_download",
                                   "last_error_code": "video_torrent",
                                   "last_error_detail": "gallery contains video torrent links",
                                   "last_error_at": None}}
        manual_context = {"entry": {"last_error_operation": "torrent_download",
                                    "last_error_code": "invalid_torrent",
                                    "last_error_detail": "下载链接返回的内容不是有效的种子文件",
                                    "last_error_at": None}}
        choices = [
            {"choice_id": "img720", "label": "[Demo] 720p archive", "suggested_role": "image",
             "size": "1.2 GiB", "seeds": 8, "posted_at": "2026-08-01", "warnings": []},
            {"choice_id": "img1080", "label": "[Demo] 1080p archive", "suggested_role": "image",
             "size": "2.4 GiB", "seeds": 3, "posted_at": "2026-07-15", "warnings": ["outdated"]},
            {"choice_id": "vid720", "label": "[Demo] 720p video", "suggested_role": "video",
             "size": "800 MiB", "seeds": 12, "posted_at": "2026-08-01", "warnings": []},
            {"choice_id": "vid1080", "label": "[Demo] 1080p video", "suggested_role": "video",
             "size": "1.6 GiB", "seeds": 0, "posted_at": "2026-07-15",
             "warnings": ["no_seeders", "resampled"]},
        ]

        def torrent(role, progress, complete=False):
            return {"role": role, "status": "completed" if complete else "downloading",
                    "progress": 1.0 if complete else progress, "speed_bps": 0 if complete else 2_400_000,
                    "updated_at": now.isoformat()}

        v1 = manga("special_processing", title="[Studio K] Summer Clips ～夏の記録～", method="torrent")
        workflow("video_archive", "active", "awaiting_torrent_selection",
                 {"entry": entry, "torrent_snapshot": {"fetched_at": now.isoformat(), "choices": choices},
                  "selection": None, "torrents": []},
                 manga_row=v1, context=video_context, jobs=[("load_torrent_options", "succeeded")],
                 progress={"message": "awaiting_torrent_selection", "total": 4})
        v2 = manga("special_processing", title="[Studio Pastel] Seaside Memories + 動画", method="torrent")
        workflow("video_archive", "active", "downloading",
                 {"entry": entry, "torrent_snapshot": {"choices": choices},
                  "selection": {"image_choice_id": "img720", "video_choice_id": "vid720"},
                  "torrents": [torrent("image", .65), torrent("video", .42)],
                  "last_checked_at": (now - timedelta(minutes=40)).isoformat()},
                 manga_row=v2, context=video_context, jobs=[("load_torrent_options", "succeeded"), ("submit_selected_torrents", "succeeded")],
                 progress={"message": "downloading", "submitted": 2, "total": 2})
        v3 = manga("special_processing", title="[Hanabira] 花火の夜 [動画付き]", method="torrent")
        workflow("video_archive", "active", "checking_downloads",
                 {"entry": entry, "torrents": [torrent("image", .98), torrent("video", .87)]},
                 manga_row=v3, context=video_context, jobs=[("check_and_compose_if_ready", "running",
                                      {"lease_token": "demo-check", "lease_owner": OWNER,
                                       "lease_until": now + timedelta(hours=2)})],
                 updated_minutes=1)
        v4 = manga("completed", title="[Moonlit Atelier] 星降る街のアトリエ", lrr_archive_id="c" * 40)
        workflow("video_archive", "completed", "ready",
                 {"entry": entry, "torrents": [torrent("image", 1, True), torrent("video", 1, True)],
                  "source_cleanup": {"status": "pending"}},
                 manga_row=v4, context=video_context, completed_at=now - timedelta(hours=20),
                 jobs=[("check_and_compose_if_ready", "succeeded")])
        v5 = manga("special_processing", title="[Kuro Neko] Autumn Leaves 動画版", method="torrent")
        workflow("video_archive", "active", "failed",
                 {"entry": entry, "torrents": [torrent("image", 1, True), torrent("video", 1, True)],
                  "retry_operation": "check_and_compose_if_ready"},
                 manga_row=v5, context=video_context, error_code="ffmpeg_failed",
                 error_detail="ffmpeg failed for video_03.mp4: conversion timed out after 300s",
                 jobs=[("check_and_compose_if_ready", "failed", {"error_code": "ffmpeg_failed",
                                                                 "error_detail": "conversion timed out"})])

        m1 = manga("special_processing", title="[Sora-iro] 空色デイズ 総集編", method="torrent")
        workflow("manual_torrent", "active", "awaiting_load", {"choices": [], "selection": None},
                 manga_row=m1, context=manual_context)
        m2 = manga("special_processing", title="[Pixel Garden] Weekend Sketches Vol.3", method="torrent")
        # 未记录错误码的人工复核（收件箱“未记录原因”分面）
        manga("manual_review", title="[Nameless] 理由不明の保留", method="direct", age_hours=7,
              manga_id="2990001/nocode0001")
        workflow("manual_torrent", "active", "awaiting_selection", {
            "loaded_at": (now - timedelta(minutes=8)).isoformat(), "selection": None,
            "choices": [
                {"choice_id": "a", "label": "[Pixel Garden] Weekend Sketches Vol.3.zip", "size": "212 MiB",
                 "size_percent": 98, "seeds": 14, "posted_at": "2026-09-20", "outdated": False, "resampled": False, "warnings": []},
                {"choice_id": "b", "label": "[Pixel Garden] Weekend Sketches Vol.3 [1280x].zip", "size": "96 MiB",
                 "size_percent": 45, "seeds": 3, "posted_at": "2026-09-21", "outdated": False, "resampled": True, "warnings": ["torrent_size_too_small"]},
                {"choice_id": "c", "label": "[Pixel Garden] Weekend Sketches Vol.3 (old).zip", "size": "205 MiB",
                 "size_percent": 95, "seeds": 0, "posted_at": "2026-08-02", "outdated": True, "resampled": False, "warnings": []},
            ]}, manga_row=m2, context=manual_context, jobs=[("load", "succeeded")])

        meta = workflow("lanraragi_metadata", "active", "awaiting_confirmation", {"count": 3}, jobs=[("scan", "succeeded")])
        meta_job = session.query(SpecialJob).filter_by(workflow_id=meta.id).first()
        preview = {"summary": {"update": 2, "verify": 1}, "results": [
            {"manga_id": "3017001/aa11bb22cc", "action": "update", "archive_id": "a" * 40, "title_changed": True,
             "actual": {"title": "旧标题"}, "expected": {"title": "[Akane] 茜色の放課後"},
             "missing_tags": ["language:chinese"], "extra_tags": ["misc:tmp"], "status": "manual_review"},
            {"manga_id": "3016002/bb22cc33dd", "action": "update", "archive_id": "b" * 40, "title_changed": False,
             "expected": {"title": "[Midori] 翠の森で"}, "missing_tags": ["artist:midori"], "extra_tags": [],
             "status": "completed"},
            {"manga_id": "3015003/cc33dd44ee", "action": "verify", "archive_id": "c" * 40, "title_changed": False,
             "expected": {"title": "[Rin] りんごの季節"}, "missing_tags": [], "extra_tags": [], "status": "completed"},
        ]}
        meta.payload = {"count": 3, "summary": preview["summary"],
                        "outputs": [_output(log_dir, meta.id, meta_job.id, "preview", preview, now)]}

        clean = workflow("download_cleanup", "active", "awaiting_confirmation", {}, jobs=[("scan", "succeeded")])
        clean_job = session.query(SpecialJob).filter_by(workflow_id=clean.id).first()
        results = [
            {"numeric_id": "3001200", "database_manga_id": "3001200/abcabcabca", "source": "qbittorrent",
             "target": "3001200", "database_status": "completed", "action": "would_delete", "detail": ""},
            {"numeric_id": "3001201", "database_manga_id": "3001201/abcabcabcb", "source": "torrent_download",
             "target": "3001201/", "database_status": "deleted", "action": "would_delete", "detail": ""},
            {"numeric_id": "3001202", "database_manga_id": "3001202/abcabcabcc", "source": "direct_download",
             "target": "3001202_x.zip", "database_status": "uploading", "action": "status_skipped",
             "detail": "status is uploading"},
            {"numeric_id": None, "database_manga_id": None, "source": "aria2_download",
             "target": "tmp-xyz.part", "database_status": None, "action": "name_not_recognized", "detail": ""},
        ]
        summary = {"would_delete": 2, "status_skipped": 1, "name_not_recognized": 1}
        clean.payload = {"summary": summary, "outputs": [
            _output(log_dir, clean.id, clean_job.id, "preview", {"summary": summary, "results": results}, now)]}

        comp = workflow("lanraragi_compare", "completed", "completed", {}, jobs=[("compare", "succeeded")],
                        completed_at=now - timedelta(hours=3))
        comp_job = session.query(SpecialJob).filter_by(workflow_id=comp.id).first()
        csum = {"database_completed_rows": 48210, "database_resolved_ids": 48208, "database_unique_ids": 48207,
                "lanraragi_archives": 48215, "lanraragi_resolved_ids": 48212, "lanraragi_unique_ids": 48210,
                "database_only": 4, "lanraragi_only": 7, "unparsed_lanraragi_archives": 3}
        report = {"summary": csum, "database_only": ["3001200", "3001201", "3001234", "2999001"],
                  "lanraragi_only": ["3011111", "3011112", "3000500", "2988000", "2988001", "2988002", "2988003"],
                  "lanraragi_only_database_states": [{"id": "3011111", "manga_id": "3011111/x", "status": "manual_review"}],
                  "database_duplicate_ids": {}, "lanraragi_duplicate_ids": {"3000500": 2},
                  "invalid_database_manga_ids": [], "unparsed_lanraragi_archives": [
                      {"arcid": "9" * 40, "title": "未知来源 A"}, {"arcid": "8" * 40, "title": "未知来源 B"}]}
        comp.payload = {"summary": csum, "lanraragi_collected_at": now.isoformat(),
                        "database_collected_at": now.isoformat(),
                        "outputs": [_output(log_dir, comp.id, comp_job.id, "report", report, now)]}

        warning = {"level": "warning", "actual": 23, "expected": 25, "site_terminal": False,
                   "url": "https://exhentai.org/?f_cats=0&next=2987650", "job_id": 3, "request_number": 288,
                   "at": (now - timedelta(hours=2)).isoformat()}
        full = workflow("full_collect", "active", "paused", {
            "round_type": "id_range", "intent": "pause", "started_at": (now - timedelta(hours=9)).isoformat(),
            "scope": {"start_id": 3001999, "end_id": 2900000, "base_url": "https://exhentai.org/",
                      "account": "browse", "initial_url": "https://exhentai.org/?f_cats=0&next=3001999",
                      "fingerprint": "demo"},
            "last_page": {"url": "https://exhentai.org/?f_cats=0&next=2984001", "first_gid": 2984000,
                          "last_gid": 2983951, "oldest_at": "2026-08-30T11:02:00+00:00",
                          "newest_at": "2026-08-30T18:47:00+00:00", "found": 25, "created": 4, "updated": 21,
                          "committed_at": (now - timedelta(minutes=31)).isoformat()},
            "cursor": "https://exhentai.org/?f_cats=0&next=2983951", "stop_reason": "user_pause", "end_reached": False,
            "counts": {"pages": 412, "requests": 431, "found": 10300, "created": 1822, "updated": 8478, "retries": 19},
            "batch_budget": {"pages": 10, "seconds": 600}, "last_batch": {"pages": 10, "seconds": 588},
            "network": {"account": "browse", "name": "proxy-hk-2", "index": 2, "pool_size": 4, "reason": "timeout"},
            "next_request_at": (now + timedelta(seconds=8)).isoformat(), "cooldown_until": "",
            "consecutive_failures": 0, "last_error": {"code": "eh_509", "at": (now - timedelta(hours=1)).isoformat()},
            "page_count_notices": {
                "count": 4, "warning_count": 3, "last_warning": warning,
                "last": {**warning, "level": "info", "actual": 12, "site_terminal": True, "request_number": 431,
                         "url": "https://exhentai.org/?f_cats=0&next=2983951",
                         "at": (now - timedelta(minutes=31)).isoformat()},
            },
        }, jobs=[("collect_batch", "succeeded"), ("collect_batch", "succeeded"), ("collect_batch", "cancelled")])
        workflow("full_collect", "active", "paused", {
            "round_type": "history", "intent": "pause", "stop_reason": "legacy_id_range_required",
            "scope": {"upper_at": "2026-09-01T00:00:00+08:00", "mode": "date", "account": "browse",
                      "base_url": "https://exhentai.org/", "initial_url": "https://exhentai.org/?next=2950000"},
            "last_page": {"url": "https://exhentai.org/?next=2941000", "oldest_at": "2026-08-12 08:00"},
            "cursor": "https://exhentai.org/?next=2940950",
            "counts": {"pages": 80, "requests": 82, "found": 2000, "created": 300, "updated": 1700, "retries": 2},
        }, jobs=[("collect_batch", "cancelled")], schema_version=1, updated_minutes=2000)

        session.commit()
    _write_logs(log_dir, full.id, now)


def build_demo(base: Path | None = None, *, management: bool = True, auth: bool = False,
               now: datetime | None = None):
    """Create database, configuration and app. Returns (app, database, base_dir)."""
    os.environ.pop("EHARCHIVE_DATABASE_URL", None)
    base = Path(base or tempfile.mkdtemp(prefix="eharchive-demo-"))
    base.mkdir(parents=True, exist_ok=True)
    if (base / "demo.db").exists():
        raise SystemExit(f"{base / 'demo.db'} 已存在；演示数据每次重新生成，请换一个空目录")
    db_url = f"sqlite:///{(base / 'demo.db').as_posix()}"
    config_dir = make_config_dir(base, db_url, auth=auth)
    database = Database(db_url)
    create_tables(database)
    seed(database, base / "logs", now)
    management_path = base / "missing-management.toml"
    if management:
        management_path = _management_preview(base, config_dir)
    from eh_archive.web.app import create_app

    return create_app(database, config_dir=config_dir, management_config=management_path), database, base


def _management_preview(base: Path, config_dir: Path) -> Path:
    from eh_archive.management.config import (
        SUPERVISOR_UNIT,
        WEB_UNIT,
        ManagementConfig,
        write_management_config,
    )
    from eh_archive.management.state import OperationStore, event, update_state
    from eh_archive.web import management

    class PreviewSystemd:
        def status(self, unit):
            active = unit == WEB_UNIT
            return {"LoadState": "loaded", "ActiveState": "active" if active else "inactive",
                    "SubState": "running" if active else "dead", "MainPID": "1234" if active else "0",
                    "ExecMainStartTimestamp": "Wed 2026-09-30 09:12:03 CST" if active else ""}

    class PreviewGit:
        def __init__(self, config):
            pass

        def inspect(self, **kwargs):
            return {"branch": "master", "remote": "origin", "old_commit": "70bf77d" + "0" * 33,
                    "old_commit_message": "6.4.10.3", "target_commit": "81ac2e9" + "0" * 33,
                    "target_commit_message": "6.4.11", "available": True, "fast_forward": True,
                    "dirty": False, "commits": "81ac2e9 6.4.11", "files": " src/eh_archive/web/app.py | 4 ++--"}

    config = ManagementConfig(repository=ROOT, config_dir=config_dir, python=Path(sys.executable),
                              management_dir=base / "management")
    path = base / "management.toml"
    write_management_config(config, path)
    state = OperationStore(config).create("git_update", "web:admin")
    update_state(state, status="succeeded", phase="completed", old_commit="a" * 40, target_commit="b" * 40,
                 previous_services={WEB_UNIT: True, SUPERVISOR_UNIT: False, "control": "paused"})
    event(state, "verify_repository", status="completed")
    (state / "operation.log").write_text("演示操作，没有改动任何服务。\n", encoding="utf-8")
    management.Systemd = PreviewSystemd
    management.GitRepository = PreviewGit
    # 运维操作（更新、重启、同步配置等）会真实执行 systemctl start，演示环境一律拒绝。
    # 新旧界面都经 POST /api/system/operations 调用此模块里的 submit。
    from eh_archive.management import ManagementError

    def blocked_submit(kind, actor, **kwargs):
        raise ManagementError(f"演示环境不执行运维操作（{kind}）", "operation_conflict")

    management.submit = blocked_submit
    # The real lock lives in /run/eharchive (created by systemd); keep previews self-contained.
    from eh_archive.management import lock

    lock.LOCK_PATH = base / "run" / "deployment.lock"
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="EH Archive 演示服务（临时 SQLite 与假数据）")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--base", type=Path, default=None, help="演示数据目录，须为空；默认新建临时目录")
    args = parser.parse_args()
    app, _, base = build_demo(args.base)
    import uvicorn

    url = f"http://127.0.0.1:{args.port}"
    print(f"Demo data: {base}\nOpen {url}/uiv3/  (also {url}/uiv2/ and {url}/ ; auth disabled)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
