from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..config import load_config
from ..db import ArchiveRepository, Database, MangaRecord
from ..db.repository import ClaimedAttempt, utcnow
from ..domain.errors import ArchiveError, ErrorClass, classify_exception
from ..domain.states import Status
from ..integrations.http import RoleSession
from ..logging import configure_logging, get_logger
from ..services.cleanup import CleanupService
from ..services.collector.parser import EhTagTranslation, parse_info
from ..services.downloader.archive import request_direct_download_url
from ..services.downloader.direct import DirectDownloader
from ..services.downloader.torrent import TorrentService, is_managed_torrent
from ..services.paths import (
    ArtifactPathService,
    UnsafePathError,
    map_external_path,
    safe_filename,
)
from ..services.preparer.zipper import prepare_directory
from ..services.uploader.lanraragi import LANraragiClient
from ..services.validator.artifact import ValidationError, quarantine_artifact, validate_artifact

log = get_logger(__name__)


def _qbit_tags(info: Any) -> set[str]:
    raw = getattr(info, "tags", None)
    if raw is None:
        try:
            raw = info["tags"]
        except (KeyError, TypeError):
            raw = ""
    if isinstance(raw, str):
        values = re.split(r"[,;]", raw)
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        values = (raw,)
    return {str(value).strip().casefold() for value in values if str(value).strip()}


def _has_qbit_tag(info: Any, tag: str) -> bool:
    return tag.casefold() in _qbit_tags(info)


def _info(record: Any):
    from ..domain.models import MangaInfo

    value = record.info
    if value is None:
        return None
    return MangaInfo(
        manga_id=value.manga_id,
        name=value.name,
        roman_name=value.roman_name,
        real_name=value.real_name,
        link=value.link,
        category=value.category,
        uploader=value.uploader,
        posted_at=value.posted_at,
        language=value.language,
        estimated_size_raw=value.estimated_size_raw,
        pages=value.pages,
        favorited=value.favorited,
        rating_count=value.rating_count,
        rating=value.rating,
        fetched_at=value.fetched_at,
        tags_raw=value.tags_raw,
        tags_translated_raw=value.tags_translated_raw,
        archive_url=None,
        parent_id=None,
    )


class TaskExecutor:
    """Execute one bounded task batch. Each task owns its attempt fencing."""

    def __init__(
        self, database: Database, *, config_dir: str | Path = "config", owner: str | None = None
    ) -> None:
        self.database = database
        self.config_dir = Path(config_dir)
        self.app, self.supervisor, self.crawl, self.secrets = load_config(config_dir)
        self.owner = owner or f"task-{os.getpid()}"
        self.paths = ArtifactPathService(self.app)
        self.system_error = False
        self._tag_translation: EhTagTranslation | None = None
        self._http_sessions: dict[str, RoleSession] = {}

    def _http_session(self, role: str) -> RoleSession:
        session = self._http_sessions.get(role)
        if session is None:
            session = RoleSession(self.app, self.secrets)
            self._http_sessions[role] = session
        return session

    def run_once(self, operation: str) -> bool:
        with self.database.session() as session:
            repository = ArchiveRepository(session)
            claim = repository.claim_next(
                operation, owner=self.owner, lease_seconds=self.supervisor.lease_seconds
            )
        if claim is None:
            return False
        # Claiming is a short transaction. The committed lease and execution
        # state must be visible before any network or filesystem work begins.
        with self.database.session() as session:
            repository = ArchiveRepository(session)
            try:
                self._execute(repository, claim)
            except Exception as exc:  # noqa: BLE001 - task boundary must classify all failures
                self._handle_error(repository, claim, exc)
        return True

    def run_batch(self, operation: str, limit: int | None = None) -> int:
        count = 0
        if limit is None:
            limit = self.supervisor.batch_size
        if limit < 0:
            raise ValueError("limit must be non-negative")
        while count < limit and self.run_once(operation):
            count += 1
            if self.system_error:
                break
        return count

    @staticmethod
    def _schedule_retry(record: MangaRecord) -> None:
        record.next_retry_at = utcnow() + timedelta(
            seconds=min(3600, 2 ** min(record.attempt_count, 8))
        )

    def _begin_external_effect(
        self, repository: ArchiveRepository, claim: ClaimedAttempt
    ) -> None:
        if not repository.begin_external_effect(claim, owner=self.owner):
            raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
        repository.session.commit()

    def _set_external_id(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, external_id: str
    ) -> None:
        if not repository.set_external_id(claim, external_id, owner=self.owner):
            raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
        repository.session.commit()

    def _fallback_torrent(
        self,
        repository: ArchiveRepository,
        claim: ClaimedAttempt,
        record: MangaRecord,
        qbit: Any,
        *,
        error_code: str,
        error_detail: str,
    ) -> None:
        current = qbit.info(record.external_download_id)
        if current is None:
            missing_hash = record.external_download_id
            record.external_download_id = None
            raise ArchiveError(
                "torrent_missing",
                f"qBittorrent no longer reports external hash {missing_hash}",
                ErrorClass.ITEM,
            )
        if not is_managed_torrent(current):
            repository.finish(claim, owner=self.owner)
            return
        self._begin_external_effect(repository, claim)
        try:
            qbit.delete(record.external_download_id, delete_files=True)
        except Exception as exc:
            raise ArchiveError(
                "torrent_fallback_cleanup",
                f"failed to delete qBittorrent task: {exc}",
                ErrorClass.ITEM,
            ) from exc
        record.download_method = self.app.fallback_method
        record.external_download_id = None
        repository.finish(
            claim,
            owner=self.owner,
            event="fallback",
            error_code=error_code,
            error_detail=error_detail,
        )

    def _execute(self, repository: ArchiveRepository, claim: ClaimedAttempt) -> None:
        record = repository.get(claim.manga_id)
        if record is None:
            raise ArchiveError("manga_missing", claim.manga_id, ErrorClass.ITEM)
        if claim.operation == "torrent_download":
            self._torrent(repository, claim, record)
        elif claim.operation == "direct_download":
            self._direct(repository, claim, record)
        elif claim.operation == "validate":
            self._validate(repository, claim, record)
        elif claim.operation == "prepare":
            self._prepare(repository, claim, record)
        elif claim.operation == "upload":
            self._upload(repository, claim, record)
        elif claim.operation == "cleanup":
            self._cleanup(repository, claim, record)
        elif claim.operation == "delete":
            self._delete(repository, claim, record)
        elif claim.operation == "details":
            self._details_task(repository, claim, record)
        else:
            raise ValueError(claim.operation)

    def _torrent(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        from ..integrations.qbittorrent import QBittorrentClient

        qbit_options = dict(self.secrets.qbittorrent)
        qbit_options.setdefault("host", self.app.qbittorrent_url)
        qbit = QBittorrentClient(**qbit_options)
        # A submitted torrent is polled by this short-lived task; it must not
        # be submitted again when the Supervisor sees downloading status.
        if record.external_download_id:
            info = qbit.info(record.external_download_id)
            if info is None:
                missing_hash = record.external_download_id
                record.external_download_id = None
                raise ArchiveError(
                    "torrent_missing",
                    f"qBittorrent no longer reports external hash {missing_hash}",
                    ErrorClass.ITEM,
                )
            if not is_managed_torrent(info):
                repository.finish(claim, owner=self.owner)
                return
            if _has_qbit_tag(info, "failed"):
                self._fallback_torrent(
                    repository,
                    claim,
                    record,
                    qbit,
                    error_code="torrent_failed",
                    error_detail="qBittorrent task was manually tagged failed",
                )
                return
            state = str(getattr(info, "state", "") or "").lower()
            if state in {"error", "missingfiles"}:
                raise ArchiveError(
                    "torrent_error",
                    f"qBittorrent task is in error state: {state}",
                    ErrorClass.ITEM,
                )
            raw_completion_on = getattr(info, "completion_on", 0) or 0
            raw_progress = getattr(info, "progress", 0) or 0
            try:
                completion_on = float(raw_completion_on)
            except (TypeError, ValueError):
                completion_on = 0.0
            if state == "stalleddl" and completion_on <= 0:
                raw_added_on = getattr(info, "added_on", 0) or 0
                try:
                    added_on = float(raw_added_on)
                except (TypeError, ValueError):
                    added_on = 0.0
                if added_on > 0 and time.time() - added_on >= self.supervisor.torrent_stall_seconds:
                    self._fallback_torrent(
                        repository,
                        claim,
                        record,
                        qbit,
                        error_code="torrent_stalled",
                        error_detail=(
                            "qBittorrent stalledDL exceeded "
                            f"{self.supervisor.torrent_stall_seconds} seconds"
                        ),
                    )
                    return
            try:
                progress = float(raw_progress)
            except (TypeError, ValueError):
                progress = 0.0
            complete = (
                completion_on > 0
                or progress >= 1
                or state
                in {
                    "uploading",
                    "stalledup",
                    "completed",
                }
            )
            if not complete:
                repository.finish(claim, owner=self.owner)
                return
            raw_external = str(getattr(info, "content_path", "") or "")
            root = self.app.root("torrent_download").resolve()
            qbit_root = self.app.qbit_torrent_path or str(root)
            try:
                raw_content = map_external_path(raw_external, qbit_root, root)
            except UnsafePathError as exc:
                raise ArchiveError("torrent_path_escape", str(exc), ErrorClass.SYSTEM) from exc
            gallery_root = root / safe_filename(record.manga_id.split("/", 1)[0])
            if not raw_content.exists():
                raise ArchiveError(
                    "torrent_content_missing",
                    f"mapped torrent content path does not exist: {raw_content}",
                    ErrorClass.ITEM,
                )
            try:
                relative = raw_content.resolve().relative_to(gallery_root)
            except ValueError as exc:
                raise ArchiveError(
                    "torrent_path_escape", str(raw_external), ErrorClass.SYSTEM
                ) from exc
            if not relative.parts:
                children = list(raw_content.iterdir()) if raw_content.is_dir() else []
                if len(children) != 1:
                    raise ArchiveError(
                        "torrent_content_ambiguous",
                        "qBittorrent content path has no unique artifact",
                        ErrorClass.ITEM,
                    )
                filename, content_path = children[0].name, children[0]
            elif len(relative.parts) != 1:
                # qBittorrent produced a nested content path. Keep only a
                # registered directory name and validate its members later.
                filename = relative.parts[0]
                content_path = gallery_root / filename
            else:
                filename, content_path = relative.name, raw_content
            fingerprint = validate_artifact(content_path, max_size=self.app.max_file_size)
            generation = (record.artifact_generation or 0) + 1
            if repository.fenced(claim, owner=self.owner) is None:
                raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
            record.artifact_location, record.artifact_filename = "torrent_download", filename
            record.artifact_kind, record.artifact_generation = fingerprint.kind, generation
            record.artifact_size = fingerprint.size
            record.artifact_sha1 = fingerprint.sha1
            record.artifact_checked_at = fingerprint.checked_at
            repository.finish(claim, owner=self.owner, event="downloaded")
            return
        if not record.torrent_link:
            record.download_method = self.app.fallback_method
            repository.finish(
                claim,
                owner=self.owner,
                event="fallback",
                error_code="no_torrent",
                error_detail="gallery has no torrent",
            )
            return
        info = _info(record)
        if info is None or not info.is_complete():
            info = self._details(record, role="browse")
            self._upsert_info_fenced(repository, claim, info)
            repository.mark_parent_outdated(info.parent_id, record.manga_id)
        if not info.is_complete():
            raise ArchiveError(
                "incomplete_details",
                "gallery details are incomplete before torrent selection",
                ErrorClass.ITEM,
            )
        http = self._http_session("browse")
        browse_network = self.secrets.network(self.app.browse_session)
        service = TorrentService(
            http=http,
            qbit=qbit,
            torrent_root=self.app.qbit_torrent_path
            or str(self.app.root("torrent_download").resolve()),
            cookies=self.secrets.cookies(self.app.browse_session),
            proxies=browse_network.get("proxies"),
        )
        self._begin_external_effect(repository, claim)
        torrent_hash, _ = service.submit(
            record.manga_id,
            record.torrent_link,
            estimated_size_raw=info.estimated_size_raw,
            skip_video=bool(record.remark and "skip video" in record.remark.lower()),
            excluded_resolutions=self.crawl.excluded_resolutions,
            video_markers=self.crawl.video_markers,
        )
        record.download_method = "torrent"
        record.external_download_id = torrent_hash
        self._set_external_id(repository, claim, torrent_hash)
        # qBittorrent owns the long-running transfer. Release this EH Archive
        # control attempt immediately while retaining downloading status.
        repository.finish(claim, owner=self.owner)

    def _details(self, record: MangaRecord, *, role: str = "archive") -> Any:
        http = self._http_session(role)
        html = http.get_text(
            record.link, role=role, timeout=self.supervisor.request_timeout_seconds
        )
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")
        except ImportError as exc:
            raise RuntimeError("beautifulsoup4 is required") from exc
        translation = self._translation(role=role)
        info, _, _ = parse_info(soup, translation)
        info.manga_id = record.manga_id
        info.link = record.link
        return info

    def _translation(self, *, role: str) -> EhTagTranslation:
        if self._tag_translation is not None:
            return self._tag_translation
        candidates = (self.config_dir / "db.text.json", Path("db.text.json"))
        for path in candidates:
            if path.exists():
                self._tag_translation = EhTagTranslation(path)
                return self._tag_translation
        try:
            payload = self._http_session("browse").get_text(
                self.crawl.tag_translation_url,
                role="browse",
                timeout=self.supervisor.request_timeout_seconds,
            )
            self._tag_translation = EhTagTranslation(data=json.loads(payload))
        except Exception:  # noqa: BLE001 - translation is optional metadata
            self._tag_translation = EhTagTranslation()
        return self._tag_translation

    def _details_task(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        info = self._details(record, role="browse")
        self._upsert_info_fenced(repository, claim, info)
        repository.mark_parent_outdated(info.parent_id, record.manga_id)
        repository.finish(claim, owner=self.owner)

    def _upsert_info_fenced(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, info: Any
    ) -> None:
        if repository.fenced(claim, owner=self.owner) is None:
            raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
        stored = repository.upsert_info(info)
        if repository.fenced(claim, owner=self.owner) is None:
            repository.session.refresh(stored)
            raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)

    def _direct(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        if record.download_method in {"hah", "aria2"} and record.external_download_id:
            self._poll_optional_download(repository, claim, record)
            return
        info = _info(record)
        if info is None or not info.archive_url:
            info = self._details(record)
            self._upsert_info_fenced(repository, claim, info)
            repository.mark_parent_outdated(info.parent_id, record.manga_id)
        if not info.archive_url:
            raise ArchiveError(
                "missing_archive_url", "details page has no archive URL", ErrorClass.ITEM
            )
        method = record.download_method or self.app.fallback_method
        if method == "hah" and not self.app.hah_enabled:
            raise ArchiveError("hah_disabled", "H@H adapter is disabled", ErrorClass.SYSTEM)
        if method == "aria2" and not self.app.aria2_enabled:
            raise ArchiveError("aria2_disabled", "aria2 adapter is disabled", ErrorClass.SYSTEM)
        if method in {"hah", "aria2"}:
            download_url = None
            if method == "aria2":
                archive_session = self._http_session("archive")
                download_url = request_direct_download_url(
                    archive_session,
                    info.archive_url,
                    timeout=self.supervisor.request_timeout_seconds,
                    headers={"Referer": record.link},
                )
            self._start_optional_download(
                repository, claim, record, info, method, download_url=download_url
            )
            return
        generation = (record.artifact_generation or 0) + 1
        paths = self.paths.for_attempt(
            manga_id=record.manga_id,
            generation=generation,
            attempt_id=claim.attempt_id,
            location="direct_download",
        )
        archive_session = self._http_session("archive")
        downloader = DirectDownloader(
            session=archive_session,
            timeout=(self.supervisor.request_timeout_seconds, 120),
            retries=self.supervisor.retry_limit,
            role="archive",
        )
        destination = paths.temporary
        self._begin_external_effect(repository, claim)
        download_url = request_direct_download_url(
            archive_session,
            info.archive_url,
            timeout=self.supervisor.request_timeout_seconds,
            headers={"Referer": record.link},
        )
        downloader.download(
            download_url,
            destination,
            headers={"User-Agent": "EH-Archive/6", "Referer": record.link},
            cookies=self.secrets.cookies(self.app.archive_session),
            proxies=self.secrets.network(self.app.archive_session).get("proxies"),
            max_size=self.app.max_file_size,
        )
        fingerprint = validate_artifact(
            destination, expected_kind="zip", max_size=self.app.max_file_size
        )
        repository_obj = repository.fenced(claim, owner=self.owner, require_generation=True)
        if repository_obj is None:
            raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
        if paths.final.exists():
            raise ArchiveError(
                "artifact_generation_exists",
                f"artifact generation already exists: {paths.final.name}",
                ErrorClass.ITEM,
            )
        os.replace(destination, paths.final)
        record = repository.get(record.manga_id)
        record.artifact_location, record.artifact_filename = "direct_download", paths.final.name
        record.artifact_kind, record.artifact_generation = "zip", generation
        record.artifact_size = fingerprint.size
        record.artifact_sha1 = fingerprint.sha1
        record.artifact_checked_at = fingerprint.checked_at
        record.download_method = "direct"
        repository.finish(claim, owner=self.owner, event="downloaded")

    def _start_optional_download(
        self,
        repository: ArchiveRepository,
        claim: ClaimedAttempt,
        record: MangaRecord,
        info: Any,
        method: str,
        *,
        download_url: str | None = None,
    ) -> None:
        generation = (record.artifact_generation or 0) + 1
        if method == "hah":
            from ..services.downloader.hah import HAHDownloader

            session = self._http_session("archive")
            adapter = HAHDownloader(
                session=session,
                root=self.app.root("hah_download"),
                cookies=self.secrets.cookies(self.app.archive_session),
                proxies=self.secrets.network(self.app.archive_session).get("proxies"),
                role="archive",
            )
            self._begin_external_effect(repository, claim)
            adapter.queue(info.archive_url)
            record.download_method, record.external_download_id = "hah", f"hah:{record.manga_id}"
            self._set_external_id(repository, claim, record.external_download_id)
            repository.finish(claim, owner=self.owner)
            return
        from ..integrations.aria2 import Aria2Adapter

        # aria2 keeps downloading after this short-lived attempt finishes.
        # Use a generation-stable pending name so a later polling attempt can
        # locate the same file without persisting an absolute path.
        _, temporary_name = self.paths.names(record.manga_id, generation, None)
        temporary = self.paths.resolve("aria2_download", temporary_name)
        adapter = Aria2Adapter(**self.secrets.network(self.app.archive_session).get("aria2", {}))
        archive_role = self.app.archive_session
        network = self.secrets.network(archive_role)
        cookies = self.secrets.cookies(archive_role)
        headers = ["User-Agent: EH-Archive/6", f"Referer: {record.link}"]
        if cookies:
            headers.append(
                "Cookie: " + "; ".join(f"{key}={value}" for key, value in cookies.items())
            )
        aria_options: dict[str, Any] = {"referer": record.link, "header": headers}
        proxies = network.get("proxies") or {}
        if isinstance(proxies, dict):
            proxy = proxies.get("https") or proxies.get("http")
            if proxy:
                aria_options["all-proxy"] = proxy
        self._begin_external_effect(repository, claim)
        gid = adapter.download(
            download_url or info.archive_url,
            directory=str(temporary.parent),
            filename=temporary.name,
            options=aria_options,
        )
        record.download_method, record.external_download_id = "aria2", gid
        self._set_external_id(repository, claim, gid)
        repository.finish(claim, owner=self.owner)

    def _poll_optional_download(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        generation = (record.artifact_generation or 0) + 1
        if record.download_method == "hah":
            from ..services.downloader.hah import HAHDownloader

            session = self._http_session("archive")
            source = HAHDownloader(
                session=session.session,
                root=self.app.root("hah_download"),
                cookies=self.secrets.cookies(self.app.archive_session),
            ).find_completed(record.manga_id)
            if source is None:
                repository.finish(claim, owner=self.owner)
                return
            fingerprint = validate_artifact(
                source, expected_kind="directory", max_size=self.app.max_file_size
            )
            if repository.fenced(claim, owner=self.owner) is None:
                raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
            record.artifact_location, record.artifact_filename, record.artifact_kind = (
                "hah_download",
                source.name,
                "directory",
            )
        else:
            from ..integrations.aria2 import Aria2Adapter

            adapter = Aria2Adapter(
                **self.secrets.network(self.app.archive_session).get("aria2", {})
            )
            if not adapter.is_complete(record.external_download_id):
                repository.finish(claim, owner=self.owner)
                return
            _, temporary_name = self.paths.names(record.manga_id, generation, None)
            source = self.paths.resolve("aria2_download", temporary_name)
            fingerprint = validate_artifact(
                source, expected_kind="zip", max_size=self.app.max_file_size
            )
            final_name, _ = self.paths.names(record.manga_id, generation, None, extension=".zip")
            final = self.paths.resolve("aria2_download", final_name)
            if final.exists():
                raise ArchiveError(
                    "artifact_generation_exists",
                    f"artifact generation already exists: {final.name}",
                    ErrorClass.ITEM,
                )
            if repository.fenced(claim, owner=self.owner) is None:
                raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
            os.replace(source, final)
            record.artifact_location, record.artifact_filename, record.artifact_kind = (
                "aria2_download",
                final.name,
                "zip",
            )
        record.artifact_generation = generation
        record.artifact_size = fingerprint.size
        record.artifact_sha1 = fingerprint.sha1
        record.artifact_checked_at = fingerprint.checked_at
        repository.finish(claim, owner=self.owner, event="downloaded")

    def _validate(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        if not record.artifact_location or not record.artifact_filename:
            raise ValidationError("missing_artifact_registration", "record has no artifact")
        path = (
            self.paths.torrent_registered(record.manga_id, record.artifact_filename)
            if record.artifact_location == "torrent_download"
            else self.paths.validate_registered(record.artifact_location, record.artifact_filename)
        )
        fingerprint = validate_artifact(
            path,
            expected_kind=record.artifact_kind,
            max_size=self.app.max_file_size,
            calculate_sha1=not bool(record.artifact_sha1),
        )
        record.artifact_size = fingerprint.size
        if fingerprint.sha1 is not None:
            record.artifact_sha1 = fingerprint.sha1
        record.artifact_checked_at = fingerprint.checked_at
        repository.finish(
            claim,
            owner=self.owner,
            event="prepare" if fingerprint.kind == "directory" else "upload",
        )

    def _prepare(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        if not record.artifact_location or not record.artifact_filename:
            raise ValidationError("missing_artifact_registration", "record has no source directory")
        source = (
            self.paths.torrent_registered(record.manga_id, record.artifact_filename)
            if record.artifact_location == "torrent_download"
            else self.paths.validate_registered(record.artifact_location, record.artifact_filename)
        )
        generation = (record.artifact_generation or 0) + 1
        paths = self.paths.for_attempt(
            manga_id=record.manga_id,
            generation=generation,
            attempt_id=claim.attempt_id,
            location="prepared",
        )

        def promote() -> None:
            if repository.fenced(claim, owner=self.owner, require_generation=True) is None:
                raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)

        result = prepare_directory(
            source,
            paths.temporary,
            paths.final,
            before_promote=promote,
        )
        if repository.fenced(claim, owner=self.owner, require_generation=True) is None:
            raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
        record.artifact_location, record.artifact_filename = "prepared", paths.final.name
        record.artifact_kind, record.artifact_generation = "zip", generation
        record.artifact_size = result.fingerprint.size
        record.artifact_sha1 = result.fingerprint.sha1
        record.artifact_checked_at = result.fingerprint.checked_at
        repository.finish(claim, owner=self.owner, event="ready")

    def _upload(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        info = _info(record)
        if info is None or not info.is_complete():
            info = self._details(record)
            self._upsert_info_fenced(repository, claim, info)
            repository.mark_parent_outdated(info.parent_id, record.manga_id)
        if not info.is_complete():
            raise ArchiveError("missing_mangainfo", "MangaInfo is incomplete", ErrorClass.ITEM)
        if not record.artifact_location or not record.artifact_filename or not record.artifact_sha1:
            raise ValidationError("missing_upload_artifact", "artifact fingerprint is incomplete")
        path = (
            self.paths.torrent_registered(record.manga_id, record.artifact_filename)
            if record.artifact_location == "torrent_download"
            else self.paths.validate_registered(record.artifact_location, record.artifact_filename)
        )
        if not path.is_file() or path.stat().st_size != record.artifact_size:
            record.artifact_sha1 = None
            record.artifact_checked_at = None
            repository.finish(claim, owner=self.owner, event="revalidate")
            return
        client = LANraragiClient(
            self.app.lanraragi_url,
            headers=self.secrets.lanraragi,
            timeout=self.supervisor.request_timeout_seconds,
        )
        self._begin_external_effect(repository, claim)
        outcome = client.upload(
            path, info, checksum=record.artifact_sha1, max_size=self.app.max_file_size
        )
        if outcome.kind == "success":
            record.lrr_archive_id = outcome.archive_id
            repository.finish(claim, owner=self.owner, event="uploaded")
        elif outcome.kind in {"retry"}:
            self._schedule_retry(record)
            repository.finish(
                claim,
                owner=self.owner,
                event="retry",
                error_code=f"lrr_{outcome.status_code}",
                error_detail=outcome.response,
            )
        elif outcome.kind == "unsupported":
            self._move_to_quarantine(repository, claim, record)
            repository.finish(
                claim,
                owner=self.owner,
                event="quarantine",
                error_code="lrr_415",
                error_detail=outcome.response,
            )
        elif outcome.kind == "revalidate":
            record.artifact_sha1 = None
            record.artifact_checked_at = None
            repository.finish(
                claim,
                owner=self.owner,
                event="revalidate",
                error_code="lrr_417",
                error_detail=outcome.response,
            )
        elif outcome.kind == "system":
            self._schedule_retry(record)
            repository.set_component(
                "upload", "paused", actor=self.owner, reason=f"LANraragi HTTP {outcome.status_code}"
            )
            repository.finish(
                claim,
                owner=self.owner,
                event="retry",
                error_code=f"lrr_{outcome.status_code}",
                error_detail=outcome.response,
            )
        elif outcome.kind == "unknown" and record.artifact_sha1:
            known = client.exists_by_sha1(record.artifact_sha1)
            if known is True:
                record.lrr_archive_id = record.artifact_sha1
                repository.finish(claim, owner=self.owner, event="uploaded")
            else:
                repository.finish(
                    claim,
                    owner=self.owner,
                    event="review",
                    error_code="lrr_upload_unknown",
                    error_detail=outcome.response,
                )
        else:
            repository.finish(
                claim,
                owner=self.owner,
                event="review",
                error_code=f"lrr_{outcome.status_code or outcome.kind}",
                error_detail=outcome.response,
            )

    def _move_to_quarantine(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        if not record.artifact_location or not record.artifact_filename:
            return
        source = (
            self.paths.torrent_registered(record.manga_id, record.artifact_filename)
            if record.artifact_location == "torrent_download"
            else self.paths.validate_registered(record.artifact_location, record.artifact_filename)
        )
        if not source.exists():
            return
        if repository.fenced(claim, owner=self.owner) is None:
            raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
        generation = (record.artifact_generation or 0) + 1
        quarantine = self.paths.for_attempt(
            manga_id=record.manga_id,
            generation=generation,
            attempt_id=claim.attempt_id,
            location="prepared",
        ).quarantine
        quarantine_artifact(source, quarantine)
        record.artifact_location = "quarantine"
        record.artifact_filename = quarantine.name
        record.artifact_generation = generation

    def _cleanup(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        if not record.lrr_archive_id:
            raise ArchiveError(
                "missing_archive_id", "cannot clean without confirmed archive ID", ErrorClass.ITEM
            )
        client = LANraragiClient(
            self.app.lanraragi_url,
            headers=self.secrets.lanraragi,
            timeout=self.supervisor.request_timeout_seconds,
        )
        status, _ = client.metadata(record.lrr_archive_id)
        if status == 400:
            raise ArchiveError(
                "lrr_archive_missing_before_cleanup",
                "LANraragi metadata returned 400 before local cleanup",
                ErrorClass.ITEM,
            )
        if status != 200:
            retryable = status in {408, 423, 429, 500, 502, 503, 504}
            category = ErrorClass.TEMPORARY if retryable else ErrorClass.SYSTEM
            raise ArchiveError(
                "lrr_metadata_check_failed",
                f"metadata returned {status}",
                category,
                retryable=retryable,
            )
        if record.artifact_location and record.artifact_filename:
            if repository.fenced(claim, owner=self.owner) is None:
                raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
            path = (
                self.paths.torrent_registered(record.manga_id, record.artifact_filename)
                if record.artifact_location == "torrent_download"
                else self.paths.validate_registered(
                    record.artifact_location, record.artifact_filename
                )
            )
            try:
                if path.exists():
                    path.unlink() if path.is_file() else shutil.rmtree(path)
            except FileNotFoundError:
                pass
        if record.download_method == "torrent" and record.external_download_id:
            from ..integrations.qbittorrent import QBittorrentClient

            self._begin_external_effect(repository, claim)
            options = dict(self.secrets.qbittorrent)
            options.setdefault("host", self.app.qbittorrent_url)
            if not CleanupService(qbit=QBittorrentClient(**options)).remove_torrent(
                record.external_download_id
            ):
                raise ArchiveError(
                    "torrent_cleanup_failed",
                    "qBittorrent task could not be removed",
                    ErrorClass.TEMPORARY,
                    retryable=True,
                )
        if record.download_method == "aria2" and record.external_download_id:
            from ..integrations.aria2 import Aria2Adapter

            self._begin_external_effect(repository, claim)
            adapter = Aria2Adapter(
                **self.secrets.network(self.app.archive_session).get("aria2", {})
            )
            if not adapter.remove(record.external_download_id):
                raise ArchiveError(
                    "aria2_cleanup_failed",
                    "aria2 task could not be removed",
                    ErrorClass.TEMPORARY,
                    retryable=True,
                )
        repository.finish(claim, owner=self.owner, event="cleanup")

    def _delete(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, record: MangaRecord
    ) -> None:
        replacement = repository.get(record.superseded_by_id) if record.superseded_by_id else None
        if replacement is None or replacement.status not in {
            Status.UPLOADED.value,
            Status.COMPLETED.value,
        }:
            raise ArchiveError(
                "replacement_not_ready", "replacement archive is not ready", ErrorClass.ITEM
            )
        if record.lrr_archive_id:
            client = LANraragiClient(
                self.app.lanraragi_url,
                headers=self.secrets.lanraragi,
                timeout=self.supervisor.request_timeout_seconds,
            )
            self._begin_external_effect(repository, claim)
            outcome = client.delete(record.lrr_archive_id)
            if outcome.kind != "deleted":
                raise ArchiveError("delete_uncertain", outcome.response, ErrorClass.ITEM)
        if record.artifact_location and record.artifact_filename:
            if repository.fenced(claim, owner=self.owner) is None:
                raise ArchiveError("stale_attempt", "attempt fencing failed", ErrorClass.TEMPORARY)
            path = (
                self.paths.torrent_registered(record.manga_id, record.artifact_filename)
                if record.artifact_location == "torrent_download"
                else self.paths.validate_registered(
                    record.artifact_location, record.artifact_filename
                )
            )
            try:
                if path.exists():
                    path.unlink() if path.is_file() else shutil.rmtree(path)
            except FileNotFoundError:
                pass
        repository.finish(claim, owner=self.owner, event="deleted")

    def _handle_error(
        self, repository: ArchiveRepository, claim: ClaimedAttempt, exc: Exception
    ) -> None:
        info = classify_exception(exc)
        if info.category == ErrorClass.SYSTEM:
            self.system_error = True
        if isinstance(exc, ValidationError) and claim.operation == "validate":
            record = repository.get(claim.manga_id)
            if record and record.artifact_location and record.artifact_filename:
                try:
                    source = (
                        self.paths.torrent_registered(record.manga_id, record.artifact_filename)
                        if record.artifact_location == "torrent_download"
                        else self.paths.validate_registered(
                            record.artifact_location, record.artifact_filename
                        )
                    )
                    generation = (record.artifact_generation or 0) + 1
                    quarantine = self.paths.for_attempt(
                        manga_id=record.manga_id,
                        generation=generation,
                        attempt_id=claim.attempt_id,
                        location="prepared",
                    ).quarantine
                    if source.exists():
                        if repository.fenced(claim, owner=self.owner) is None:
                            return
                        quarantine_artifact(source, quarantine)
                        record.artifact_location, record.artifact_filename = (
                            "quarantine",
                            quarantine.name,
                        )
                        record.artifact_generation = generation
                        record.artifact_size = record.artifact_sha1 = None
                        record.artifact_checked_at = None
                        repository.finish(
                            claim,
                            owner=self.owner,
                            event="quarantine",
                            error_code=exc.code,
                            error_detail=str(exc),
                        )
                    else:
                        repository.finish(
                            claim,
                            owner=self.owner,
                            event="review",
                            error_code=exc.code,
                            error_detail=str(exc),
                        )
                    return
                except (OSError, ValueError):
                    pass
        if info.retryable and info.category == ErrorClass.TEMPORARY:
            record = repository.get(claim.manga_id)
            if (
                record
                and claim.operation != "details"
                and record.attempt_count >= max(1, self.supervisor.retry_limit)
            ):
                try:
                    repository.finish(
                        claim,
                        owner=self.owner,
                        event="review",
                        error_code="retry_limit_exceeded",
                        error_detail=info.message,
                    )
                except ValueError:
                    repository.finish(
                        claim,
                        owner=self.owner,
                        status=Status.MANUAL_REVIEW,
                        error_code="retry_limit_exceeded",
                        error_detail=info.message,
                    )
                return
            if record:
                record.next_retry_at = utcnow() + timedelta(
                    seconds=min(3600, 2 ** min(record.attempt_count, 8))
                )
            retry_event = {
                "details": "details_retry",
                "cleanup": "cleanup_retry",
                "delete": "review",
            }.get(claim.operation, "retry")
            if not repository.finish(
                claim,
                owner=self.owner,
                event=retry_event,
                error_code=info.code,
                error_detail=info.message,
            ):
                return
        else:
            event = "review"
            if claim.operation == "details":
                # Details are auxiliary until upload. Keep the download
                # route runnable and retry metadata independently.
                record = repository.get(claim.manga_id)
                if record:
                    record.next_retry_at = utcnow() + timedelta(
                        seconds=min(3600, 2 ** min(record.attempt_count, 8))
                    )
                event = "details_retry"
            if claim.operation != "details" and info.code in {
                "gallery_unavailable",
                "archive_unavailable",
                "http_unavailable",
            }:
                event = "unavailable"
            if info.code in {
                "no_torrent",
                "no_seeded_torrent",
                "only_outdated_torrents",
                "only_resampled_torrents",
            }:
                event = "fallback"
                if claim.operation == "torrent_download":
                    record = repository.get(claim.manga_id)
                    if record:
                        record.download_method = self.app.fallback_method
            try:
                repository.finish(
                    claim,
                    owner=self.owner,
                    event=event,
                    error_code=info.code,
                    error_detail=info.message,
                )
            except ValueError:
                repository.finish(
                    claim,
                    owner=self.owner,
                    status=Status.MANUAL_REVIEW,
                    error_code=info.code,
                    error_detail=info.message,
                )
        log.exception(
            "task failed",
            extra={"event": {"manga_id": claim.manga_id, "operation": claim.operation}},
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eharchive-task")
    parser.add_argument("--operation", required=True)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    app, _, _, _ = load_config(args.config_dir)
    configure_logging(app.log_level, app.log_dir)
    executor = TaskExecutor(Database(app.database_url), config_dir=args.config_dir)
    executor.run_batch(args.operation, args.limit)
    return 2 if executor.system_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
