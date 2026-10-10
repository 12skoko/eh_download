from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode
from fastapi import Query, Request
from fastapi.responses import HTMLResponse
from ..logs import list_logs, resolve_log


def register(app, templates, context, root: Path):
    root = root.resolve()

    @app.get("/v1/logs", response_class=HTMLResponse)
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
            name="v1/logs.html",
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

    @app.get("/v1/logs/view", response_class=HTMLResponse)
    def log_page(request: Request, file: str):
        resolve_log(root, file)
        return templates.TemplateResponse(
            request=request,
            name="v1/log_view.html",
            context=context(
                request,
                filename=file,
                file_query=quote(file, safe=""),
                directory_query=urlencode({"directory": str(PurePosixPath(file).parent)}),
            ),
            headers={"Cache-Control": "no-store"},
        )
