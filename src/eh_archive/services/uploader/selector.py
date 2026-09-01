from __future__ import annotations

HTTP_BACKEND = "lanraragi_http"
FILESYSTEM_BACKEND = "lanraragi_filesystem"
UPLOAD_BACKEND_MODES = frozenset({"http", "filesystem", "auto"})


def select_upload_backend(mode: str, *, artifact_size: int, threshold_bytes: int) -> str:
    """Choose a backend without performing I/O or consulting the database."""

    if mode not in UPLOAD_BACKEND_MODES:
        raise ValueError(f"unsupported upload_backend: {mode!r}")
    if artifact_size < 0:
        raise ValueError("artifact_size must not be negative")
    if threshold_bytes < 0:
        raise ValueError("large_upload_threshold_bytes must not be negative")
    if mode == "http":
        return HTTP_BACKEND
    if mode == "filesystem":
        return FILESYSTEM_BACKEND
    if threshold_bytes > 0 and artifact_size >= threshold_bytes:
        return FILESYSTEM_BACKEND
    return HTTP_BACKEND

