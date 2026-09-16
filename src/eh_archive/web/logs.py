from __future__ import annotations

import os
import stat
import tempfile
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

WINDOW_BYTES = 64 * 1024
MAX_ENTRIES = 20000


def build_log_archive(root: Path):
    """Build on temporary disk storage; never follow links or chase growing files."""
    if not root.is_dir():
        raise HTTPException(404, "日志目录不存在或不可读取")
    # Ownership passes to the streaming response, which closes the file after download.
    archive = tempfile.TemporaryFile()  # noqa: SIM115
    try:
        with zipfile.ZipFile(
            archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as bundle:
            pending = [root]
            while pending:
                directory = pending.pop()
                if _linked(directory) or not directory.resolve().is_relative_to(root):
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        if _linked(path) or not path.resolve().is_relative_to(root):
                            continue
                        name = path.relative_to(root).as_posix()
                        if entry.is_dir(follow_symlinks=False):
                            bundle.writestr(name + "/", b"")
                            pending.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            with path.open("rb") as source:
                                remaining = os.fstat(source.fileno()).st_size
                                with bundle.open(name, "w", force_zip64=True) as destination:
                                    while remaining:
                                        data = source.read(min(WINDOW_BYTES, remaining))
                                        if not data:
                                            break
                                        destination.write(data)
                                        remaining -= len(data)
        size = archive.tell()
        archive.seek(0)
        return archive, size
    except BaseException:
        archive.close()
        raise


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def resolve_log(root: Path, name: str, *, directory: bool = False) -> Path:
    parts = PurePosixPath(name).parts
    if (
        not parts
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or any(part in {".", ".."} for part in name.split("/"))
        or (not directory and PurePosixPath(name).suffix.lower() not in {".log", ".jsonl"})
    ):
        raise HTTPException(404, "日志文件不存在")
    root = root.resolve()
    path = root
    try:
        for part in parts:
            path = path / part
            if _linked(path):
                raise HTTPException(404, "不支持链接文件或目录")
        if not path.resolve().is_relative_to(root) or not (
            path.is_dir() if directory else path.is_file()
        ):
            raise HTTPException(404, "日志文件不存在")
    except OSError:
        raise HTTPException(404, "日志文件不存在或不可读取") from None
    return path


def list_logs(root: Path, query: str = "", directory: str = "") -> tuple[list[dict], bool]:
    rows = []
    scanned = 0
    pending = [resolve_log(root, directory, directory=True) if directory else root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_ENTRIES:
                        return sorted(rows, key=lambda row: row["mtime"], reverse=True), True
                    path = Path(entry.path)
                    try:
                        if _linked(path):
                            continue
                        is_directory = entry.is_dir(follow_symlinks=False)
                        if is_directory or entry.is_file(follow_symlinks=False):
                            name = path.relative_to(root).as_posix()
                            if not is_directory and path.suffix.lower() not in {".log", ".jsonl"}:
                                continue
                            if query.casefold() not in path.name.casefold():
                                continue
                            info = entry.stat(follow_symlinks=False)
                            rows.append(
                                {
                                    "name": name,
                                    "is_directory": is_directory,
                                    "directory": PurePosixPath(name).parent.as_posix(),
                                    "filename": path.name,
                                    "size": info.st_size,
                                    "mtime": info.st_mtime,
                                    "modified": datetime.fromtimestamp(info.st_mtime, UTC),
                                    "url": quote(name, safe=""),
                                }
                            )
                    except OSError:
                        continue
        except OSError:
            continue
    return sorted(rows, key=lambda row: row["mtime"], reverse=True), False


def read_window(path: Path, before: int | None = None) -> dict:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        end = min(before, size) if before is not None else size
        start = max(0, end - WINDOW_BYTES)
        handle.seek(start)
        data = handle.read(end - start)
    return {
        "text": data.decode("utf-8", errors="replace"),
        "start": start,
        "end": start + len(data),
        "size": size,
    }


def register(app, templates, context, root: Path):
    root = root.resolve()
    archive_lock = threading.Lock()

    @app.get("/api/logs/archive")
    def archive_download():
        from starlette.background import BackgroundTask

        if not archive_lock.acquire(blocking=False):
            raise HTTPException(409, "正在打包日志，请稍后重试")
        try:
            handle, size = build_log_archive(root)
        except FileNotFoundError:
            raise HTTPException(409, "打包期间文件发生变化，请重试") from None
        except OSError:
            raise HTTPException(503, "无法打包日志，请检查读取权限和临时目录可用空间") from None
        finally:
            archive_lock.release()

        def chunks():
            try:
                while data := handle.read(WINDOW_BYTES):
                    yield data
            finally:
                handle.close()

        filename = f"logs_{datetime.now(UTC):%Y%m%d_%H%M%S}_UTC.zip"
        return StreamingResponse(
            chunks(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(size),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(handle.close),
        )

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request, q: str = "", directory: str = "", page: int = Query(1, ge=1)):
        directory = "" if directory == "." else directory
        rows, truncated = list_logs(root, q, directory)
        rows.sort(
            key=lambda row: (
                not row["is_directory"],
                0 if row["is_directory"] else -row["mtime"],
                row["name"],
            )
        )
        parts = PurePosixPath(directory).parts
        breadcrumbs = [
            {"name": part, "url": quote("/".join(parts[: index + 1]), safe="")}
            for index, part in enumerate(parts)
        ]
        parent = "/".join(parts[:-1])
        pages = max(1, (len(rows) + 99) // 100)
        page = min(page, pages)
        return templates.TemplateResponse(
            request=request,
            name="logs.html",
            context=context(
                request,
                rows=rows[(page - 1) * 100 : page * 100],
                q=q,
                filter_query=urlencode({"q": q, "directory": directory}),
                breadcrumbs=breadcrumbs,
                parent_query=urlencode({"directory": parent}),
                reset_query=urlencode({"directory": directory}),
                directory=directory,
                page=page,
                pages=pages,
                total=len(rows),
                truncated=truncated,
                available=root.is_dir(),
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/logs/view", response_class=HTMLResponse)
    def log_page(request: Request, file: str):
        resolve_log(root, file)
        return templates.TemplateResponse(
            request=request,
            name="log_view.html",
            context=context(
                request,
                filename=file,
                file_query=quote(file, safe=""),
                directory_query=urlencode({"directory": str(PurePosixPath(file).parent)}),
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/logs/content")
    def content(file: str, before: int | None = Query(None, ge=0)):
        from fastapi.responses import JSONResponse

        path = resolve_log(root, file)
        try:
            return JSONResponse(read_window(path, before), headers={"Cache-Control": "no-store"})
        except OSError:
            raise HTTPException(404, "日志已移除或不可读取") from None

    @app.get("/api/logs/download")
    def download(file: str):
        path = resolve_log(root, file)
        try:
            handle = path.open("rb")
            size = os.fstat(handle.fileno()).st_size
        except OSError:
            raise HTTPException(404, "日志已移除或不可读取") from None

        def chunks():
            try:
                remaining = size
                while remaining:
                    data = handle.read(min(WINDOW_BYTES, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
            finally:
                handle.close()

        from starlette.background import BackgroundTask

        return StreamingResponse(
            chunks(),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(handle.close),
        )
