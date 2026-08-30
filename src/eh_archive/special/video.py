from __future__ import annotations

import copy
import shutil
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ..config import load_config, load_video_archive_config
from ..db import Database
from ..db.models import MangaRecord, SpecialWorkflow
from ..domain.errors import ArchiveError, ErrorClass
from ..integrations.http import RoleSession
from ..logging import get_logger
from ..services.downloader.torrent import parse_torrent_options, torrent_category
from ..services.paths import (
    ArtifactPathService,
    UnsafePathError,
    external_path_key,
    map_external_path,
    safe_manga_id,
)
from ..services.validator import validate_artifact
from .archive_tools import (
    VideoArchiveError,
    build_legacy_layout,
    prepare_deterministic_zip,
    safe_extract_zip,
    unique_zip,
    validate_compose_dependencies,
)
from .registry import (
    CANCEL_VIDEO_ARCHIVE,
    CHECK_AND_COMPOSE,
    LOAD_TORRENT_OPTIONS,
    SUBMIT_SELECTED_TORRENTS,
    get_operation,
)
from .repository import (
    ClaimedSpecialJob,
    SpecialCancellationRequested,
    SpecialRepository,
)

log = get_logger(__name__)


def _value(item: Any, name: str, default: Any = None) -> Any:
    value = getattr(item, name, None)
    if value is None:
        try:
            value = item[name]
        except (KeyError, TypeError):
            value = default
    return default if value is None else value


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _complete(info: Any) -> bool:
    state = str(_value(info, "state", "")).casefold()
    return bool(
        _float(_value(info, "completion_on", 0)) > 0
        or _float(_value(info, "progress", 0)) >= 1
        or state in {"uploading", "stalledup", "completed", "pausedup", "queuedup"}
    )


class VideoArchiveExecutor:
    def __init__(
        self,
        database: Database,
        *,
        config_dir: str | Path,
        claim: ClaimedSpecialJob,
        qbit: Any | None = None,
        http: Any | None = None,
    ) -> None:
        self.database = database
        self.config_dir = Path(config_dir)
        self.claim = claim
        self.app, self.supervisor, self.crawl, self.secrets = load_config(self.config_dir)
        self.module = load_video_archive_config(self.config_dir)
        if not self.module.enabled and claim.operation != CANCEL_VIDEO_ARCHIVE:
            raise ArchiveError(
                "special_module_disabled", "video_archive module is disabled", ErrorClass.SYSTEM
            )
        self._qbit = qbit
        self._http = http

    @property
    def qbit(self) -> Any:
        if self._qbit is None:
            from ..integrations.qbittorrent import QBittorrentClient

            options = dict(self.secrets.qbittorrent)
            options.setdefault("host", self.app.qbittorrent_url)
            self._qbit = QBittorrentClient(**options)
        return self._qbit

    @property
    def http(self) -> Any:
        if self._http is None:
            self._http = RoleSession(self.app, self.secrets)
        return self._http

    def run(self) -> None:
        try:
            if self.claim.operation != CANCEL_VIDEO_ARCHIVE:
                _, _, payload = self._state()
                if payload.get("cancel_requested"):
                    raise SpecialCancellationRequested
            if self.claim.operation == LOAD_TORRENT_OPTIONS:
                self.load_torrent_options()
            elif self.claim.operation == SUBMIT_SELECTED_TORRENTS:
                self.submit_selected_torrents()
            elif self.claim.operation == CHECK_AND_COMPOSE:
                self.check_and_compose_if_ready()
            elif self.claim.operation == CANCEL_VIDEO_ARCHIVE:
                self.cancel_video_archive()
            else:
                raise ArchiveError(
                    "unsupported_special_operation", self.claim.operation, ErrorClass.SYSTEM
                )
        except SpecialCancellationRequested:
            self.cancel_video_archive()

    def _state(self) -> tuple[MangaRecord, SpecialWorkflow, dict[str, Any]]:
        with self.database.session() as session:
            values = SpecialRepository(session, timezone=self.app.timezone).validate_claim(
                self.claim.job_id,
                workflow_id=self.claim.workflow_id,
                lease_token=self.claim.lease_token,
                lease_owner=self.claim.lease_owner,
            )
            if values is None:
                raise ArchiveError(
                    "stale_special_job", "special job fencing failed", ErrorClass.TEMPORARY
                )
            _, workflow, manga = values
            session.expunge(workflow)
            session.expunge(manga)
            return manga, workflow, copy.deepcopy(workflow.payload or {})

    def _begin_external(self) -> None:
        with self.database.session() as session:
            if not SpecialRepository(session).begin_external_effect(self.claim):
                raise ArchiveError(
                    "stale_special_job", "special job fencing failed", ErrorClass.TEMPORARY
                )

    def _update(
        self,
        *,
        payload: dict[str, Any] | None = None,
        progress: dict[str, Any] | None = None,
        phase: str | None = None,
        event_type: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.database.session() as session:
            repository = SpecialRepository(session, timezone=self.app.timezone)
            operation = get_operation(self.claim.kind, self.claim.operation)
            lease_seconds = operation.lease_seconds or self.supervisor.special_job_lease_seconds
            if not repository.renew(self.claim, lease_seconds=lease_seconds):
                raise ArchiveError(
                    "stale_special_job", "special job fencing failed", ErrorClass.TEMPORARY
                )
            if not repository.update_state(
                self.claim,
                payload=payload,
                progress=progress,
                phase=phase,
                event_type=event_type,
                detail=detail,
            ):
                raise ArchiveError(
                    "stale_special_job", "special job fencing failed", ErrorClass.TEMPORARY
                )

    def _succeed(
        self,
        *,
        phase: str,
        payload: dict[str, Any],
        progress: dict[str, Any],
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.database.session() as session:
            if not SpecialRepository(session, timezone=self.app.timezone).succeed(
                self.claim,
                phase=phase,
                payload=payload,
                progress=progress,
                detail=detail,
            ):
                raise ArchiveError(
                    "stale_special_job", "special job fencing failed", ErrorClass.TEMPORARY
                )

    def _fetch_options(self, manga: MangaRecord):
        if not manga.torrent_link:
            raise ArchiveError("no_torrent", "gallery has no torrent page", ErrorClass.ITEM)
        response = self.http.get(
            manga.torrent_link,
            role="browse",
            timeout=self.supervisor.request_timeout_seconds,
        )
        response.raise_for_status()
        text = response.text
        if "This gallery is currently unavailable" in text:
            raise ArchiveError("gallery_unavailable", "gallery is unavailable", ErrorClass.ITEM)
        return parse_torrent_options(
            text,
            excluded_resolutions=self.crawl.excluded_resolutions,
            video_markers=self.crawl.video_markers,
        )

    def load_torrent_options(self) -> None:
        manga, _, payload = self._state()
        self._begin_external()
        options = self._fetch_options(manga)
        payload["torrent_snapshot"] = {
            "fetched_at": datetime.now(UTC).isoformat(),
            "choices": [item.public_snapshot() for item in options],
        }
        payload.pop("selection", None)
        payload.pop("retry_operation", None)
        self._succeed(
            phase="awaiting_torrent_selection",
            payload=payload,
            progress={"message": "awaiting_torrent_selection", "total": len(options)},
            detail={"choice_count": len(options)},
        )

    def _special_paths(self, manga_id: str, role: str) -> tuple[str, Path, str]:
        safe_id = safe_manga_id(manga_id)
        external_root, local_root = self._download_roots()
        separator = (
            "\\"
            if "\\" in external_root and "/" not in external_root
            else "/"
        )
        external = external_root.rstrip("\\/")
        external_path = f"{external}{separator}{safe_id}{separator}{role}"
        local_path = local_root / safe_id / role
        display_name = f"{manga_id.split('/', 1)[0]}-{role}"
        return external_path, local_path, display_name

    def _download_roots(self) -> tuple[str, Path]:
        """Return the shared qBittorrent and local roots from app.toml."""

        local_root = self.app.root("torrent_download").resolve()
        external_root = self.app.qbit_torrent_path or str(local_root)
        return external_root, local_root

    def _download_torrent(self, manga: MangaRecord, option: Any) -> bytes:
        response = self.http.get(
            urljoin(manga.torrent_link, option.url),
            role="browse",
            timeout=self.supervisor.request_timeout_seconds,
        )
        response.raise_for_status()
        content = bytes(response.content)
        if content.startswith(b"The torrent file could not be found") or not content.startswith(
            b"d"
        ):
            raise ArchiveError(
                "invalid_torrent", "torrent response is not a bencode dictionary", ErrorClass.ITEM
            )
        return content

    def submit_selected_torrents(self) -> None:
        manga, _, payload = self._state()
        selection = dict(payload.get("selection") or {})
        image_id = str(selection.get("image_choice_id", ""))
        video_id = str(selection.get("video_choice_id", ""))
        if not image_id or not video_id or image_id == video_id:
            raise ArchiveError(
                "invalid_torrent_selection",
                "one different image and video torrent must be selected",
                ErrorClass.ITEM,
            )
        self._begin_external()
        current = self._fetch_options(manga)
        by_id = {item.choice_id: item for item in current}
        if image_id not in by_id or video_id not in by_id:
            raise ArchiveError(
                "torrent_selection_stale",
                "selected torrent is no longer present; reload the candidate list",
                ErrorClass.ITEM,
            )
        selected = {"image": by_id[image_id], "video": by_id[video_id]}
        confirmed = set(selection.get("confirmed_warnings") or [])
        required = {
            f"{role}:{warning}" for role, option in selected.items() for warning in option.warnings
        }
        if not required.issubset(confirmed):
            raise ArchiveError(
                "torrent_selection_stale",
                "selected torrent risks changed; reload and confirm the new warnings",
                ErrorClass.ITEM,
            )
        torrents = {
            str(item.get("role")): dict(item)
            for item in payload.get("torrents", [])
            if isinstance(item, dict) and item.get("role") in {"image", "video"}
        }
        for role in ("image", "video"):
            if torrents.get(role, {}).get("external_id"):
                continue
            option = selected[role]
            external_path, _, display_name = self._special_paths(manga.manga_id, role)
            existing = self.qbit.find_owned(
                category=self.module.download.category,
                save_path=external_path,
                display_name=display_name,
            )
            if existing is not None:
                torrent_hash = str(_value(existing, "hash", ""))
            else:
                collision = self.qbit.find_owned(
                    category=self.module.download.category,
                    save_path=external_path,
                )
                if collision is not None:
                    raise ArchiveError(
                        "special_torrent_path_conflict",
                        f"the owned {role} save path is already used by another torrent",
                        ErrorClass.ITEM,
                    )
                content = self._download_torrent(manga, option)
                torrent_hash = self.qbit.add(
                    content,
                    save_path=external_path,
                    display_name=display_name,
                    category=self.module.download.category,
                )
            if not torrent_hash:
                raise ArchiveError(
                    "torrent_hash_not_found",
                    f"qBittorrent did not report the {role} hash",
                    ErrorClass.TEMPORARY,
                )
            torrents[role] = {
                "role": role,
                "provider": "qbittorrent",
                "external_id": torrent_hash,
                "status": "submitted",
                "progress": 0.0,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            payload["torrents"] = [torrents[key] for key in ("image", "video") if key in torrents]
            self._update(
                payload=payload,
                progress={"message": "submitting_torrents", "submitted": len(torrents), "total": 2},
                event_type="special_torrent_submitted",
                detail={"role": role},
            )
        payload["torrents"] = [torrents[role] for role in ("image", "video")]
        payload.pop("retry_operation", None)
        self._succeed(
            phase="downloading",
            payload=payload,
            progress={"message": "downloading", "submitted": 2, "total": 2},
            detail={"roles": ["image", "video"]},
        )

    def _torrent_snapshot(self, role: str, info: Any) -> dict[str, Any]:
        complete = _complete(info)
        progress = max(0.0, min(1.0, _float(_value(info, "progress", 0))))
        total_size = max(0, _int(_value(info, "total_size", _value(info, "size", 0))))
        downloaded = max(0, _int(_value(info, "downloaded", round(total_size * progress))))
        return {
            "role": role,
            "provider": "qbittorrent",
            "external_id": str(_value(info, "hash", "")),
            "status": "completed" if complete else str(_value(info, "state", "downloading")),
            "qbit_state": str(_value(info, "state", "")),
            "progress": 1.0 if complete else progress,
            "total_size": total_size,
            "downloaded_bytes": downloaded,
            "speed_bps": max(0, _int(_value(info, "dlspeed", 0))),
            "eta_seconds": max(0, _int(_value(info, "eta", 0))),
            "completion_time": _int(_value(info, "completion_on", 0)) or None,
            "content_path": str(_value(info, "content_path", "")) if complete else None,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def check_and_compose_if_ready(self) -> None:
        manga, _, payload = self._state()
        self._begin_external()
        stored = {
            str(item.get("role")): dict(item)
            for item in payload.get("torrents", [])
            if isinstance(item, dict)
        }
        if set(stored) != {"image", "video"}:
            raise ArchiveError(
                "special_torrent_state_missing", "both torrent hashes are required", ErrorClass.ITEM
            )
        snapshots: dict[str, dict[str, Any]] = {}
        infos: dict[str, Any] = {}
        for role in ("image", "video"):
            torrent_hash = str(stored[role].get("external_id", ""))
            info = self.qbit.info(torrent_hash)
            if info is None:
                raise ArchiveError(
                    "special_torrent_missing",
                    f"qBittorrent no longer reports the {role} torrent",
                    ErrorClass.ITEM,
                )
            if torrent_category(info) != self.module.download.category:
                raise ArchiveError(
                    "special_torrent_ownership_lost",
                    f"the {role} torrent category no longer belongs to this module",
                    ErrorClass.ITEM,
                )
            expected_external, _, _ = self._special_paths(manga.manga_id, role)
            observed_save_path = str(_value(info, "save_path", ""))
            if external_path_key(observed_save_path) != external_path_key(expected_external):
                raise ArchiveError(
                    "special_torrent_path_changed",
                    f"the {role} torrent save path no longer matches its owned path",
                    ErrorClass.ITEM,
                )
            infos[role] = info
            snapshots[role] = self._torrent_snapshot(role, info)
        payload["torrents"] = [snapshots[role] for role in ("image", "video")]
        payload["last_checked_at"] = datetime.now(UTC).isoformat()
        complete_count = sum(1 for item in snapshots.values() if item["status"] == "completed")
        self._update(
            payload=payload,
            progress={"message": "checking_downloads", "completed": complete_count, "total": 2},
            event_type="special_download_checked",
            detail={"completed": complete_count, "total": 2},
        )
        if complete_count != 2:
            self._succeed(
                phase="downloading",
                payload=payload,
                progress={"message": "downloading", "completed": complete_count, "total": 2},
                detail={"ready": False},
            )
            return
        local_content: dict[str, Path] = {}
        external_root, local_root = self._download_roots()
        for role in ("image", "video"):
            raw = str(_value(infos[role], "content_path", ""))
            try:
                local_content[role] = map_external_path(
                    raw,
                    external_root,
                    local_root,
                )
            except UnsafePathError as exc:
                raise ArchiveError(
                    "special_torrent_path_escape", str(exc), ErrorClass.SYSTEM
                ) from exc
            if not local_content[role].exists():
                raise ArchiveError(
                    "special_torrent_content_missing",
                    f"completed {role} torrent content is unavailable locally",
                    ErrorClass.ITEM,
                )
        self._compose(manga, payload, local_content)

    def _compose(
        self,
        manga: MangaRecord,
        payload: dict[str, Any],
        local_content: dict[str, Path],
    ) -> None:
        snapshot = dict(payload.get("config_snapshot") or {})
        effective_config = replace(
            self.module,
            ffmpeg=replace(
                self.module.ffmpeg,
                quality=int(snapshot.get("webp_quality", self.module.ffmpeg.quality)),
                compression_level=int(
                    snapshot.get("webp_compression_level", self.module.ffmpeg.compression_level)
                ),
                loop=int(snapshot.get("webp_loop", self.module.ffmpeg.loop)),
            ),
            output=replace(
                self.module.output,
                include_original_mp4=bool(
                    snapshot.get("include_original_mp4", self.module.output.include_original_mp4)
                ),
                layout=str(snapshot.get("layout", self.module.output.layout)),
            ),
        )
        if effective_config.output.layout != "legacy_folders":
            raise ArchiveError(
                "special_config_snapshot_invalid",
                "workflow output layout is no longer supported",
                ErrorClass.ITEM,
            )
        validate_compose_dependencies(
            effective_config,
            download_root=self.app.root("torrent_download"),
        )
        generation = (self.claim.artifact_generation or 0) + 1
        workspace_root = self.module.work.workspace_root.resolve()
        job_root = (
            workspace_root
            / safe_manga_id(manga.manga_id)
            / f"w{self.claim.workflow_id}"
            / f"g{generation}"
        )
        try:
            job_root.resolve().relative_to(workspace_root)
        except ValueError as exc:
            raise ArchiveError("special_workspace_escape", str(exc), ErrorClass.SYSTEM) from exc
        image_source = job_root / "source_image"
        video_source = job_root / "source_video"
        output_root = job_root / "output"
        for controlled_path in (image_source, video_source, output_root):
            try:
                controlled_path.resolve().relative_to(workspace_root)
            except ValueError as exc:
                raise ArchiveError(
                    "special_workspace_escape",
                    "special workspace contains a path outside its configured root",
                    ErrorClass.SYSTEM,
                ) from exc
        job_root.mkdir(parents=True, exist_ok=True)
        self._update(
            phase="extracting",
            progress={"message": "extracting", "completed": 0, "total": 2},
            event_type="special_extract_started",
        )
        image_zip = unique_zip(local_content["image"])
        video_zip = unique_zip(local_content["video"])
        safe_extract_zip(image_zip, image_source, limits=self.module.safety)
        self._update(progress={"message": "extracting", "completed": 1, "total": 2})
        safe_extract_zip(video_zip, video_source, limits=self.module.safety)
        self._update(
            phase="converting",
            progress={"message": "converting", "completed": 0},
            event_type="special_convert_started",
        )
        last_progress = 0.0

        def conversion_progress(completed: int, total: int, current: str) -> None:
            nonlocal last_progress
            now = time.monotonic()
            if completed != total and now - last_progress < 2:
                return
            last_progress = now
            self._update(
                progress={
                    "message": "converting",
                    "completed": completed,
                    "total": total,
                    "current_file": current,
                }
            )

        counts = build_legacy_layout(
            image_source,
            video_source,
            output_root,
            config=effective_config,
            progress=conversion_progress,
        )
        self._update(
            phase="packing",
            progress={"message": "packing", **counts},
            event_type="special_pack_started",
        )
        paths = ArtifactPathService(self.app).for_attempt(
            manga_id=manga.manga_id,
            generation=generation,
            attempt_id=self.claim.job_id,
            location="prepared",
            extension=".zip",
        )

        def before_promote() -> None:
            with self.database.session() as session:
                values = SpecialRepository(session).validate_claim(
                    self.claim.job_id,
                    workflow_id=self.claim.workflow_id,
                    lease_token=self.claim.lease_token,
                    lease_owner=self.claim.lease_owner,
                )
                if (
                    values is None
                    or values[2].artifact_generation != self.claim.artifact_generation
                ):
                    raise ArchiveError(
                        "stale_special_job", "final artifact fencing failed", ErrorClass.TEMPORARY
                    )
                if (values[1].payload or {}).get("cancel_requested"):
                    raise SpecialCancellationRequested

        if paths.final.exists():
            existing = validate_artifact(paths.final, expected_kind="zip")
            candidate = paths.final.with_name(f".{paths.final.name}.j{self.claim.job_id}.candidate")
            candidate.unlink(missing_ok=True)
            try:
                expected = prepare_deterministic_zip(
                    output_root,
                    paths.temporary,
                    candidate,
                    before_promote=before_promote,
                )
                if existing.size != expected.size or existing.sha1 != expected.sha1:
                    raise VideoArchiveError(
                        "artifact_generation_conflict",
                        "an existing artifact generation does not match the rebuilt output",
                    )
            finally:
                candidate.unlink(missing_ok=True)
            fingerprint = existing
        else:
            fingerprint = prepare_deterministic_zip(
                output_root,
                paths.temporary,
                paths.final,
                before_promote=before_promote,
            )
        payload["source_cleanup"] = (
            "requested_on_success" if self.module.output.cleanup_source_on_success else "retained"
        )
        payload["workspace"] = {
            "generation": generation,
            "job_id": self.claim.job_id,
            "counts": counts,
        }
        with self.database.session() as session:
            if not SpecialRepository(session, timezone=self.app.timezone).complete_video_archive(
                self.claim,
                payload=payload,
                fingerprint=fingerprint,
                generation=generation,
                detail={"source_cleanup": payload["source_cleanup"], **counts},
            ):
                raise ArchiveError(
                    "stale_special_job",
                    "final artifact database fencing failed",
                    ErrorClass.TEMPORARY,
                )
        if self.module.output.cleanup_source_on_success:
            self._cleanup_after_success(payload, job_root)

    def _cleanup_after_success(self, payload: dict[str, Any], job_root: Path) -> None:
        detail: dict[str, Any] = {"deleted": [], "skipped": [], "workspace_removed": False}
        failures: list[str] = []
        try:
            detail.update(self._cleanup_qbit(payload))
        except Exception as exc:
            failures.append(error_code(exc)[0])
            log.exception(
                "post-completion qBittorrent cleanup failed: job_id=%s", self.claim.job_id
            )
        try:
            if job_root.exists():
                shutil.rmtree(job_root)
            detail["workspace_removed"] = True
        except OSError as exc:
            failures.append(error_code(exc)[0])
            log.exception("post-completion workspace cleanup failed: job_id=%s", self.claim.job_id)
        if failures:
            status = "failed"
        elif detail["skipped"]:
            status = "partial"
        else:
            status = "completed"
        if failures:
            detail["error_codes"] = sorted(set(failures))
        with self.database.session() as session:
            SpecialRepository(session, timezone=self.app.timezone).record_source_cleanup(
                self.claim.workflow_id,
                status=status,
                detail=detail,
                error_code=failures[0] if failures else None,
            )

    def _cleanup_qbit(self, payload: dict[str, Any]) -> dict[str, Any]:
        deleted: list[str] = []
        skipped: list[str] = []
        for item in payload.get("torrents", []):
            if not isinstance(item, dict) or not item.get("external_id"):
                continue
            role = str(item.get("role", "unknown"))
            info = self.qbit.info(str(item["external_id"]))
            if info is None:
                continue
            if torrent_category(info) != self.module.download.category:
                skipped.append(role)
                continue
            expected_path, _, _ = self._special_paths(self.claim.manga_id, role)
            observed_path = str(_value(info, "save_path", ""))
            if external_path_key(observed_path) != external_path_key(expected_path):
                skipped.append(role)
                continue
            self.qbit.delete(str(item["external_id"]), delete_files=True)
            deleted.append(role)
        return {"deleted": deleted, "skipped": skipped}

    def cancel_video_archive(self) -> None:
        _, _, payload = self._state()
        self._begin_external()
        cleanup = self._cleanup_qbit(payload)
        root = self.module.work.workspace_root.resolve()
        workflow_root = root / safe_manga_id(self.claim.manga_id) / f"w{self.claim.workflow_id}"
        try:
            workflow_root.resolve().relative_to(root)
        except ValueError as exc:
            raise ArchiveError("special_workspace_escape", str(exc), ErrorClass.SYSTEM) from exc
        if workflow_root.exists():
            shutil.rmtree(workflow_root)
        with self.database.session() as session:
            if not SpecialRepository(session, timezone=self.app.timezone).cancel_complete(
                self.claim,
                detail={"torrent_cleanup": cleanup, "workspace_removed": True},
            ):
                raise ArchiveError(
                    "stale_special_job", "special job fencing failed", ErrorClass.TEMPORARY
                )


def error_code(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, VideoArchiveError):
        return exc.code, str(exc)
    if isinstance(exc, ArchiveError):
        return exc.info.code, exc.info.message
    return "unexpected_special_error", str(exc) or type(exc).__name__
