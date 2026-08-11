from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...domain.errors import ArchiveError, ErrorClass
from ...domain.models import MangaInfo


@dataclass(frozen=True)
class UploadOutcome:
    kind: str
    archive_id: str | None = None
    status_code: int | None = None
    response: str = ""


def build_tags(info: MangaInfo, *, date_added: int | None = None) -> str:
    date_added = date_added or int(datetime.now(UTC).timestamp())
    fields = {
        "romaname": info.roman_name,
        "source": info.link,
        "category": info.category,
        "uploader": info.uploader,
        "postedtime": info.posted_at.isoformat() if info.posted_at else "",
        "language": info.language,
        "pages": info.pages or 0,
        "favorited": info.favorited or 0,
        "ratingcount": info.rating_count or 0,
        "rating": info.rating or 0,
        "updatetime": int(info.fetched_at.timestamp()) if info.fetched_at else 0,
        "date_added": date_added,
    }
    metadata = ",".join(f"{key}:{str(value).replace(',', '，')}" for key, value in fields.items())
    return ",".join(x for x in (metadata, info.tags_translated_raw, info.tags_raw) if x)


class LANraragiClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        session: Any | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.session = session
        self.timeout = timeout

    def _session(self):
        if self.session is None:
            import requests

            self.session = requests.Session()
        return self.session

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            return getattr(self._session(), method)(url, **kwargs)
        except Exception as exc:
            name = type(exc).__name__.lower()
            if isinstance(exc, OSError) or any(
                term in name for term in ("connection", "timeout", "refused", "reset")
            ):
                raise ArchiveError(
                    "lanraragi_unavailable",
                    f"LANraragi is unavailable: {exc}",
                    ErrorClass.SYSTEM,
                ) from exc
            raise

    def upload(
        self, path: str | Path, info: MangaInfo, *, checksum: str, max_size: int | None = None
    ) -> UploadOutcome:
        path = Path(path)
        size = path.stat().st_size
        if max_size is not None and size > max_size:
            return UploadOutcome("too_large", status_code=413, response=str(size))
        if not info.is_complete():
            raise ArchiveError("missing_mangainfo", "MangaInfo is incomplete", ErrorClass.ITEM)
        data = {"title": info.name, "tags": build_tags(info), "file_checksum": checksum}
        try:
            from requests_toolbelt.multipart.encoder import MultipartEncoder
        except ImportError as exc:
            raise ArchiveError(
                "upload_dependency_missing",
                "requests-toolbelt is required for streaming uploads",
                ErrorClass.SYSTEM,
            ) from exc
        with path.open("rb") as handle:
            fields = {**data, "file": (path.name, handle, "application/zip")}
            encoder = MultipartEncoder(fields=fields)
            headers = {**self.headers, "Content-Type": encoder.content_type}
            response = self._request(
                "put",
                f"{self.base_url}/api/archives/upload",
                data=encoder,
                headers=headers,
                timeout=self.timeout,
            )
        body = response.text[:4000]
        status = int(response.status_code)
        if status == 200:
            try:
                payload = response.json()
            except Exception as exc:
                raise ArchiveError(
                    "upload_invalid_response",
                    "LANraragi returned non-JSON success",
                    ErrorClass.ITEM,
                ) from exc
            archive_id = str(payload.get("id", ""))
            success = payload.get("success") in {1, "1"} and payload.get("operation") == "upload"
            if (
                not success
                or not re.fullmatch(r"[0-9a-fA-F]{40}", archive_id)
                or archive_id.lower() != checksum.lower()
            ):
                raise ArchiveError(
                    "upload_missing_archive_id",
                    "success response has no SHA1 archive ID",
                    ErrorClass.ITEM,
                )
            try:
                metadata_status, _ = self.metadata(archive_id)
            except Exception as exc:  # noqa: BLE001 - upload result is now uncertain
                return UploadOutcome(
                    "unknown",
                    archive_id,
                    None,
                    f"upload accepted but metadata confirmation failed: {exc}",
                )
            if metadata_status != 200:
                if metadata_status in {401, 403}:
                    return UploadOutcome(
                        "system",
                        archive_id,
                        metadata_status,
                        "upload accepted but metadata authentication was rejected",
                    )
                return UploadOutcome(
                    "unknown",
                    archive_id,
                    metadata_status,
                    "upload accepted but metadata confirmation failed",
                )
            return UploadOutcome("success", archive_id, status, body)
        if status == 409:
            return UploadOutcome("duplicate_review", status_code=status, response=body)
        if status in {423, 429}:
            return UploadOutcome("retry", status_code=status, response=body)
        if status in {500, 502, 503, 504}:
            return UploadOutcome("unknown", status_code=status, response=body)
        if status == 415:
            return UploadOutcome("unsupported", status_code=status, response=body)
        if status in {401, 403}:
            return UploadOutcome("system", status_code=status, response=body)
        if status == 417:
            return UploadOutcome("revalidate", status_code=status, response=body)
        if status in {400, 422}:
            return UploadOutcome("review", status_code=status, response=body)
        return UploadOutcome("unknown", status_code=status, response=body)

    def metadata(self, archive_id: str) -> tuple[int, dict[str, Any] | None]:
        response = self._request(
            "get",
            f"{self.base_url}/api/archives/{archive_id}/metadata",
            headers=self.headers,
            timeout=self.timeout,
        )
        try:
            value = response.json()
        except ValueError:
            value = None
        return int(response.status_code), value

    def exists_by_sha1(self, checksum: str) -> bool | None:
        try:
            status, _payload = self.metadata(checksum)
        except ArchiveError as exc:
            if exc.info.category == ErrorClass.SYSTEM:
                raise
            return None
        except Exception:  # noqa: BLE001 - an unknown result must never trigger a blind retry
            return None
        if status == 200:
            return True
        if status == 400 or status == 404:
            return False
        if status in {401, 403}:
            raise ArchiveError(
                "lanraragi_authentication_failed",
                f"LANraragi rejected metadata authentication with HTTP {status}",
                ErrorClass.SYSTEM,
            )
        return None

    def delete(self, archive_id: str) -> UploadOutcome:
        response = self._request(
            "delete",
            f"{self.base_url}/api/archives/{archive_id}",
            headers=self.headers,
            timeout=self.timeout,
        )
        if response.status_code in {200, 204}:
            try:
                payload = response.json() if response.text else {"success": 1}
            except ValueError:
                return UploadOutcome(
                    "review", status_code=int(response.status_code), response=response.text[:1000]
                )
            if payload.get("success") not in {1, "1"}:
                return UploadOutcome(
                    "review", status_code=int(response.status_code), response=response.text[:1000]
                )
            verify_status, _ = self.metadata(archive_id)
            if verify_status == 400:
                return UploadOutcome(
                    "deleted", status_code=int(response.status_code), response=response.text[:1000]
                )
            return UploadOutcome(
                "unknown", status_code=verify_status, response="delete response was not confirmed"
            )
        if response.status_code in {423, 429, 500, 502, 503, 504}:
            return UploadOutcome(
                "retry", status_code=int(response.status_code), response=response.text[:1000]
            )
        if response.status_code in {401, 403}:
            return UploadOutcome(
                "system", status_code=int(response.status_code), response=response.text[:1000]
            )
        return UploadOutcome(
            "review", status_code=int(response.status_code), response=response.text[:1000]
        )

    def regenerate_all_thumbnails(self) -> UploadOutcome:
        response = self._request(
            "post",
            f"{self.base_url}/api/regen_thumbs?force=0",
            headers=self.headers,
            timeout=self.timeout,
        )
        if response.status_code in {200, 202, 204}:
            return UploadOutcome(
                "accepted", status_code=int(response.status_code), response=response.text[:1000]
            )
        if response.status_code in {423, 429, 500, 502, 503, 504}:
            return UploadOutcome(
                "retry", status_code=int(response.status_code), response=response.text[:1000]
            )
        if response.status_code in {401, 403}:
            return UploadOutcome(
                "system", status_code=int(response.status_code), response=response.text[:1000]
            )
        return UploadOutcome(
            "review", status_code=int(response.status_code), response=response.text[:1000]
        )
