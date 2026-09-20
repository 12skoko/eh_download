from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...domain.errors import ArchiveError, ErrorClass
from ...domain.models import MangaInfo
from .contracts import UploadOutcome

RETRYABLE_HTTP_STATUSES = frozenset({408, 423, 429, 500, 502, 503, 504})
AUTH_HTTP_STATUSES = frozenset({401, 403})
LANRARAGI_ID_PREFIX_BYTES = 512_000


def tag_values(value: str) -> tuple[str, ...]:
    """Keep tag text intact while ignoring empty entries and duplicate tags."""
    return tuple(dict.fromkeys(tag.strip() for tag in value.split(",") if tag.strip()))


def comparison_tags(value: str) -> dict[str, str]:
    """Index tags by NFC for comparison, retaining original text for reports."""
    tags = {}
    for tag in tag_values(value):
        tags.setdefault(unicodedata.normalize("NFC", tag), tag)
    return tags


def metadata_differences(payload: Mapping[str, Any], expected: Mapping[str, str]) -> list[str]:
    mismatched = []
    for key, value in expected.items():
        actual = payload.get(key)
        equal = actual == value
        if key == "title" and isinstance(actual, str):
            equal = unicodedata.normalize("NFC", actual) == unicodedata.normalize("NFC", value)
        if key == "tags":
            equal = isinstance(actual, str) and (
                comparison_tags(actual).keys() == comparison_tags(value).keys()
            )
        if not equal:
            mismatched.append(key)
    return mismatched


def lanraragi_archive_id(path: str | Path) -> str:
    """Return SHA-1 of exactly the first 512000 bytes (or all of a short file)."""

    with Path(path).open("rb") as stream:
        return hashlib.sha1(stream.read(LANRARAGI_ID_PREFIX_BYTES)).hexdigest()


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
    return ",".join(tag_values(",".join(
        x for x in (metadata, info.tags_translated_raw, info.tags_raw) if x
    )))


class LANraragiApiGateway:
    """Shared LANraragi API access used by upload backends and maintenance tasks."""

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

    def _session(self) -> Any:
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
                    f"LANraragi is unavailable ({type(exc).__name__})",
                    ErrorClass.SYSTEM,
                ) from exc
            raise

    @staticmethod
    def _payload(response: Any) -> dict[str, Any] | None:
        try:
            value = response.json()
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _body(response: Any, limit: int = 4000) -> str:
        return str(getattr(response, "text", ""))[:limit]

    @staticmethod
    def metadata_values(info: MangaInfo) -> dict[str, str]:
        # MangaInfo has no summary field. Do not invent one or clear a remote
        # summary; future callers can extend this mapping explicitly.
        return {"title": info.name, "tags": build_tags(info)}

    def list_archives(self) -> list[dict[str, Any]]:
        response = self._request("get", f"{self.base_url}/api/archives",
                                 headers=self.headers, timeout=self.timeout)
        if response.status_code != 200:
            raise ArchiveError("lanraragi_compare_http_error",
                f"LANraragi archive list returned HTTP {response.status_code}", ErrorClass.ITEM)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ValueError("LANraragi 返回非法 JSON") from exc
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("LANraragi 档案响应不是有效列表")
        return payload

    def upload_archive(
        self,
        path: str | Path,
        info: MangaInfo,
        *,
        checksum: str,
        timeout: float | tuple[float, float] | None = None,
    ) -> UploadOutcome:
        path = Path(path)
        if not info.is_complete():
            raise ArchiveError("missing_mangainfo", "MangaInfo is incomplete", ErrorClass.ITEM)
        data = {**self.metadata_values(info), "file_checksum": checksum}
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
                timeout=self.timeout if timeout is None else timeout,
            )
        body = self._body(response)
        status = int(response.status_code)
        if status == 200:
            payload = self._payload(response)
            if payload is None:
                raise ArchiveError(
                    "upload_invalid_response",
                    "LANraragi returned non-JSON success",
                    ErrorClass.ITEM,
                )
            archive_id = str(payload.get("id", ""))
            success = payload.get("success") in {1, "1"} and payload.get("operation") == "upload"
            if not success or not re.fullmatch(r"[0-9a-fA-F]{40}", archive_id):
                raise ArchiveError(
                    "upload_missing_archive_id",
                    "success response has no valid archive ID",
                    ErrorClass.ITEM,
                )
            return UploadOutcome("success", archive_id, status, body)
        if status == 409:
            return UploadOutcome(
                "duplicate_review", status_code=status, response=body, error_code="lrr_duplicate"
            )
        if status in {423, 429}:
            return UploadOutcome(
                "retry", status_code=status, response=body, error_code=f"lrr_{status}"
            )
        if status in {500, 502, 503, 504}:
            return UploadOutcome(
                "unknown", status_code=status, response=body, error_code="lrr_upload_unknown"
            )
        if status == 415:
            return UploadOutcome(
                "unsupported", status_code=status, response=body, error_code="lrr_415"
            )
        if status in AUTH_HTTP_STATUSES:
            return UploadOutcome(
                "system",
                status_code=status,
                response=body,
                error_code="lanraragi_authentication_failed",
            )
        if status == 417:
            return UploadOutcome(
                "revalidate", status_code=status, response=body, error_code="lrr_417"
            )
        if status in {400, 422}:
            return UploadOutcome(
                "review", status_code=status, response=body, error_code=f"lrr_{status}"
            )
        return UploadOutcome(
            "unknown", status_code=status, response=body, error_code="lrr_upload_unknown"
        )

    # Compatibility entry point. Task execution uses HttpUploadBackend so both
    # upload variants share metadata update and confirmation semantics.
    def upload(
        self,
        path: str | Path,
        info: MangaInfo,
        *,
        checksum: str,
        timeout: float | tuple[float, float] | None = None,
    ) -> UploadOutcome:
        return self.upload_archive(path, info, checksum=checksum, timeout=timeout)

    def metadata(self, archive_id: str) -> tuple[int, dict[str, Any] | None]:
        response = self._request(
            "get",
            f"{self.base_url}/api/archives/{archive_id}/metadata",
            headers={"Accept": "application/json", **self.headers},
            timeout=self.timeout,
            allow_redirects=False,
        )
        return int(response.status_code), self._payload(response)

    def update_metadata(
        self,
        archive_id: str,
        info: MangaInfo,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> UploadOutcome:
        request_headers = {"Accept": "application/json", **self.headers}
        response = self._request(
            "put",
            f"{self.base_url}/api/archives/{archive_id}/metadata",
            params=dict(metadata) if metadata is not None else self.metadata_values(info),
            headers=request_headers,
            timeout=self.timeout,
            allow_redirects=False,
        )
        status = int(response.status_code)
        body = self._body(response)
        location = str((getattr(response, "headers", {}) or {}).get("Location") or "")
        if location:
            body = f"redirect_location: {location}\n{body}"
        payload = self._payload(response)
        if status == 200 and payload is not None and payload.get("success") in {1, "1"}:
            return UploadOutcome("success", archive_id, status, body)
        if status in AUTH_HTTP_STATUSES:
            return UploadOutcome(
                "system", archive_id, status, body, "lanraragi_authentication_failed"
            )
        if status in RETRYABLE_HTTP_STATUSES:
            return UploadOutcome("retry", archive_id, status, body, f"lrr_metadata_{status}")
        if status == 200 and payload is None:
            return UploadOutcome(
                "review", archive_id, status, body, "lrr_metadata_non_json_response"
            )
        return UploadOutcome("review", archive_id, status, body, "lrr_metadata_update_failed")

    @staticmethod
    def metadata_matches_artifact(
        payload: Mapping[str, Any] | None,
        *,
        archive_id: str,
        size: int,
        filename: str,
    ) -> bool:
        if payload is None:
            return False
        actual_id = str(payload.get("arcid") or payload.get("id") or "")
        try:
            actual_size = int(payload.get("size"))
        except (TypeError, ValueError):
            return False
        actual_filename = str(payload.get("filename") or "")
        extension = str(payload.get("extension") or "").lstrip(".")
        # LANraragi separates the final extension from the filename; the stem
        # may itself end in the same suffix (for example, archive.zip.zip).
        if extension:
            actual_filename = f"{actual_filename}.{extension}"
        return (
            actual_id == archive_id
            and actual_size == size
            and actual_filename == filename
        )

    def confirm_metadata(
        self,
        archive_id: str,
        *,
        size: int,
        filename: str,
        expected: Mapping[str, str],
    ) -> UploadOutcome:
        status, payload = self.metadata(archive_id)
        if status in AUTH_HTTP_STATUSES:
            return UploadOutcome(
                "system",
                archive_id,
                status,
                "metadata authentication was rejected",
                "lanraragi_authentication_failed",
            )
        if status in RETRYABLE_HTTP_STATUSES or status in {400, 404}:
            return UploadOutcome(
                "retry",
                archive_id,
                status,
                "archive metadata is not available for confirmation",
                "lrr_metadata_not_ready",
            )
        if status != 200 or not self.metadata_matches_artifact(
            payload, archive_id=archive_id, size=size, filename=filename
        ):
            return UploadOutcome(
                "review",
                archive_id,
                status,
                "archive ID, size, or filename did not match metadata",
                "lrr_metadata_artifact_mismatch",
            )
        mismatched = metadata_differences(payload, expected)
        if mismatched:
            return UploadOutcome(
                "review",
                archive_id,
                status,
                "metadata fields did not match after update: " + ", ".join(mismatched),
                "lrr_metadata_mismatch",
            )
        return UploadOutcome("success", archive_id, status)

    def shinobu_status(self) -> dict[str, Any]:
        response = self._request(
            "get",
            f"{self.base_url}/api/shinobu",
            headers=self.headers,
            timeout=self.timeout,
        )
        status = int(response.status_code)
        payload = self._payload(response)
        if status in AUTH_HTTP_STATUSES:
            raise ArchiveError(
                "lanraragi_authentication_failed",
                "LANraragi rejected Shinobu authentication",
                ErrorClass.SYSTEM,
            )
        if (
            status != 200
            or payload is None
            or payload.get("success") not in {1, "1"}
            or payload.get("is_alive") not in {1, "1"}
        ):
            raise ArchiveError(
                "shinobu_unavailable",
                f"Shinobu is not running (HTTP {status})",
                ErrorClass.SYSTEM,
            )
        return payload

    def info(self) -> tuple[int, dict[str, Any] | None]:
        response = self._request(
            "get",
            f"{self.base_url}/api/info",
            headers=self.headers,
            timeout=self.timeout,
        )
        return int(response.status_code), self._payload(response)

    def delete(self, archive_id: str) -> UploadOutcome:
        response = self._request(
            "delete",
            f"{self.base_url}/api/archives/{archive_id}",
            headers=self.headers,
            timeout=self.timeout,
        )
        status = int(response.status_code)
        body = self._body(response, 1000)
        if status in {200, 204}:
            payload = self._payload(response) if body else {"success": 1}
            if payload is None or payload.get("success") not in {1, "1"}:
                return UploadOutcome("review", status_code=status, response=body)
            verify_status, _ = self.metadata(archive_id)
            if verify_status == 400:
                return UploadOutcome("deleted", status_code=status, response=body)
            return UploadOutcome(
                "unknown", status_code=verify_status, response="delete response was not confirmed"
            )
        if status in RETRYABLE_HTTP_STATUSES:
            return UploadOutcome("retry", status_code=status, response=body)
        if status in AUTH_HTTP_STATUSES:
            return UploadOutcome("system", status_code=status, response=body)
        return UploadOutcome("review", status_code=status, response=body)

    def regenerate_all_thumbnails(self) -> UploadOutcome:
        response = self._request(
            "post",
            f"{self.base_url}/api/regen_thumbs?force=0",
            headers=self.headers,
            timeout=self.timeout,
        )
        status = int(response.status_code)
        body = self._body(response, 1000)
        if status in {200, 202, 204}:
            return UploadOutcome("accepted", status_code=status, response=body)
        if status in RETRYABLE_HTTP_STATUSES:
            return UploadOutcome("retry", status_code=status, response=body)
        if status in AUTH_HTTP_STATUSES:
            return UploadOutcome("system", status_code=status, response=body)
        return UploadOutcome("review", status_code=status, response=body)


LANraragiClient = LANraragiApiGateway
