"""Archive list, triage inbox, dossier and every per-archive write action."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select

from ...db.models import MangaRecord
from ...services.downloader.torrent.review import WARNING_LABELS as TORRENT_WARNING_LABELS
from ...special.core.service import ModuleService, SpecialInvalidRequest, SpecialServiceError
from ...special.service import (
    SpecialWorkflowService,
    active_special_workflow,
    special_entry_for_manga,
)
from ..app import (
    _actor,
    _filter_query,
    _manual_artifact_directories,
    _optional_text,
    _validated_form,
)
from ..services import (
    InvalidRequest,
    WebService,
    WebServiceError,
    list_manga,
    list_review_manga,
    manga_detail,
    manga_progress_data,
    review_facets,
)
from .common import PREFIX, TOASTS, Uiv2, is_htmx, is_partial, manga_url, redirect, trigger_header

# Actions that move the archive elsewhere in the pipeline; the triage pane may
# then open the next item. Remark/priority edits keep the current one.
_ADVANCING = {
    "video-skipped", "action-completed", "expired-lease-released", "conflict-versions-resolved",
    "conflict-rename-requested", "status-updated", "torrent-warning-updated",
}


def install(app, ctx: Uiv2) -> None:
    database = ctx.database
    app_config = ctx.app_config
    config_dir = ctx.config_dir

    # ------------------------------------------------------------------ 列表
    @app.get(PREFIX + "/archives", response_class=HTMLResponse)
    def archives(
        request: Request, q: str | None = None,
        uploader: str | None = None, tags: str | None = None, queue_source: str | None = None,
        has_error: str | None = None, limit: int = 50, page: int = 1,
    ):
        status = request.query_params.getlist("status")
        error_filter = None if has_error not in {"yes", "no"} else has_error == "yes"
        try:
            with database.session() as session:
                result = list_manga(
                    session, statuses=status, query_text=q, uploader=uploader, tags=tags,
                    queue_source=queue_source, has_error=error_filter, limit=limit, page=page,
                )
        except WebServiceError as exc:
            return ctx.service_error(request, exc)
        params = [(k, v) for k, v in (("q", q), ("uploader", uploader), ("tags", tags),
                                      ("queue_source", queue_source), ("has_error", has_error)) if v]
        params.append(("limit", str(limit)))
        params.extend(("status", s) for s in status)
        values = {
            "page": result, "selected_statuses": status, "q": q or "", "uploader": uploader or "",
            "tags": tags or "", "queue_source": queue_source or "", "has_error": has_error or "",
            "limit": limit, "filter_qs": _filter_query(params),
        }
        if is_partial(request, "archive-results"):
            return ctx.partial(request, "uiv2/_archive_results.html", **values)
        return ctx.page(request, "uiv2/archives.html", **values)

    @app.get(PREFIX + "/archives/status-counts")
    def status_counts():
        with database.session() as session:
            counts = dict(session.execute(
                select(MangaRecord.status, func.count()).group_by(MangaRecord.status)
            ).all())
        return JSONResponse(counts, headers={"Cache-Control": "no-store"})

    @app.get(PREFIX + "/inbox", response_class=HTMLResponse)
    def inbox(
        request: Request, status: str = "manual_review", q: str | None = None,
        error_code: str | None = None, operation: str | None = None, page: int = 1,
        missing_error: bool = False,
    ):
        if status not in {"manual_review", "quarantined"}:
            status = "manual_review"
        if missing_error:
            error_code = None
        try:
            with database.session() as session:
                result = list_review_manga(
                    session, status=status, query_text=q, error_code=error_code,
                    missing_error=missing_error, operation=operation, limit=50, page=page,
                )
                facets, operations = review_facets(
                    session, status=status, query_text=q, operation=operation, include_missing=True,
                )
                other = "quarantined" if status == "manual_review" else "manual_review"
                totals = {
                    status: result.total if not (q or error_code or operation or missing_error) else None,
                    other: session.scalar(
                        select(func.count()).select_from(MangaRecord).where(MangaRecord.status == other)
                    ),
                }
                if totals[status] is None:
                    totals[status] = session.scalar(
                        select(func.count()).select_from(MangaRecord).where(MangaRecord.status == status)
                    )
        except WebServiceError as exc:
            return ctx.service_error(request, exc)
        params = [("status", status)]
        params += [(k, v) for k, v in (("q", q), ("operation", operation), ("error_code", error_code)) if v]
        if missing_error:
            params.append(("missing_error", "true"))
        values = {
            "page": result, "selected_status": status, "q": q or "", "error_code": error_code or "",
            "missing_error": missing_error,
            "operation": operation or "", "error_facets": facets, "review_operations": operations,
            "filter_qs": _filter_query(params), "totals": totals,
        }
        if is_partial(request, "inbox-list"):
            return ctx.partial(request, "uiv2/_inbox_list.html", oob_facets=True, **values)
        return ctx.page(request, "uiv2/inbox.html", **values)

    # ------------------------------------------------------------------ 档案详情
    def dossier_values(manga_id: str, mode: str) -> dict:
        with database.session() as session:
            detail = manga_detail(session, manga_id)
            row = detail["row"]
            workflow = (
                active_special_workflow(session, row.manga_id)
                if row.status == "special_processing" else None
            )
        return dict(
            **detail, mode=mode, special_workflow=workflow,
            special_entry=special_entry_for_manga(row, config_dir),
            torrent_warning_labels=TORRENT_WARNING_LABELS,
            artifact_directories=_manual_artifact_directories(app_config, manga_id),
        )

    def mode_of(value) -> str:
        return "pane" if value == "pane" else "page"

    @app.get(PREFIX + "/pane/{manga_id:path}", response_class=HTMLResponse)
    def pane(request: Request, manga_id: str):
        try:
            values = dossier_values(manga_id, "pane")
        except WebServiceError as exc:
            return ctx.error(request, str(exc), exc.status_code)
        return ctx.partial(request, "uiv2/_dossier.html", **values)

    @app.get(PREFIX + "/partials/dossier/{manga_id:path}", response_class=HTMLResponse)
    def dossier_partial(request: Request, manga_id: str, mode: str = "page"):
        try:
            values = dossier_values(manga_id, mode_of(mode))
        except WebServiceError as exc:
            return ctx.error(request, str(exc), exc.status_code)
        return ctx.partial(request, "uiv2/_dossier.html", **values)

    @app.get(PREFIX + "/partials/progress/{manga_id:path}", response_class=HTMLResponse)
    def progress_partial(request: Request, manga_id: str, mode: str = "page"):
        try:
            with database.session() as session:
                values = manga_progress_data(session, manga_id)
        except WebServiceError as exc:
            return ctx.error(request, str(exc), exc.status_code)
        return ctx.partial(request, "uiv2/_progress.html", mode=mode_of(mode), **values)

    @app.get(PREFIX + "/archives/{manga_id:path}", response_class=HTMLResponse)
    def dossier_page(request: Request, manga_id: str):
        try:
            values = dossier_values(manga_id, "page")
        except WebServiceError as exc:
            return ctx.service_error(request, exc)
        return ctx.page(request, "uiv2/dossier.html", **values)

    # ------------------------------------------------------------------ 写操作
    def update(request, form, manga_id, callback, key, *, with_config=False):
        try:
            with database.session() as session:
                callback(WebService(
                    session, actor=_actor(request), app_config=app_config if with_config else None
                ))
        except (ValueError, WebServiceError) as exc:
            error = exc if isinstance(exc, WebServiceError) else InvalidRequest("表单数据无效")
            return ctx.service_error(request, error)
        if not is_htmx(request):
            return redirect(request, manga_url(manga_id), key)
        mode = mode_of(form.get("mode"))
        headers = trigger_header(
            toast=TOASTS.get(key, "操作已完成"),
            changed={"manga_id": manga_id, "advance": key in _ADVANCING},
        )
        try:
            values = dossier_values(manga_id, mode)
        except WebServiceError as exc:
            return ctx.error(request, str(exc), exc.status_code)
        return ctx.partial(request, "uiv2/_dossier.html", headers=headers, **values)

    def version(form) -> int:
        return int(str(form.get("row_version", "")))

    @app.post(PREFIX + "/archives/{manga_id:path}/remark")
    async def remark(request: Request, manga_id: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.update_remark(
            manga_id, remark=_optional_text(form.get("remark")), row_version=version(form),
        ), "remark-updated")

    @app.post(PREFIX + "/archives/{manga_id:path}/priority")
    async def priority(request: Request, manga_id: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.update_priority(
            manga_id, priority=int(str(form.get("priority", ""))), row_version=version(form),
        ), "priority-updated")

    @app.post(PREFIX + "/archives/{manga_id:path}/skip-video")
    async def skip_video(request: Request, manga_id: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.skip_video_and_resume(
            manga_id, row_version=version(form),
        ), "video-skipped")

    @app.post(PREFIX + "/archives/{manga_id:path}/actions/{action}")
    async def action(request: Request, manga_id: str, action: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.action(
            manga_id, action, row_version=version(form),
            reason=_optional_text(form.get("reason")), archive_id=_optional_text(form.get("archive_id")),
        ), "action-completed")

    @app.post(PREFIX + "/archives/{manga_id:path}/lease/release-expired")
    async def release_lease(request: Request, manga_id: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.release_expired_lease(
            manga_id, row_version=version(form), reason=_optional_text(form.get("reason")),
            confirmed=form.get("confirmed") == "yes",
        ), "expired-lease-released")

    @app.post(PREFIX + "/archives/{manga_id:path}/conflict-versions")
    async def conflict_versions(request: Request, manga_id: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.resolve_conflict_versions(
            manga_id, row_version=version(form),
            old_versions={str(key): int(str(form.get(f"version:{key}", "")))
                          for key in form.getlist("old_ids")},
            target_status=str(form.get("target_status", "")),
            reason=_optional_text(form.get("reason")), confirmed=form.get("confirmed") == "yes",
        ), "conflict-versions-resolved", with_config=True)

    @app.post(PREFIX + "/archives/{manga_id:path}/conflict-rename")
    async def conflict_rename(request: Request, manga_id: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.request_conflict_rename(
            manga_id, row_version=version(form),
            target_filename=_optional_text(form.get("target_filename")),
            reason=_optional_text(form.get("reason")), confirmed=form.get("confirmed") == "yes",
        ), "conflict-rename-requested", with_config=True)

    @app.post(PREFIX + "/archives/{manga_id:path}/status/{target_status}")
    async def override_status(request: Request, manga_id: str, target_status: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.override_status(
            manga_id, target_status=target_status, row_version=version(form),
            reason=_optional_text(form.get("reason")),
            download_method=_optional_text(form.get("download_method")),
            artifact_filename=_optional_text(form.get("artifact_filename")),
            archive_id=_optional_text(form.get("archive_id")),
            superseded_by_id=_optional_text(form.get("superseded_by_id")),
            confirmation_manga_id=_optional_text(form.get("confirmation_manga_id")),
            allow_web_only=True, config_dir=config_dir,
        ), "status-updated", with_config=True)

    @app.post(PREFIX + "/archives/{manga_id:path}/torrent-link-permission")
    async def torrent_link_permission(request: Request, manga_id: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.set_torrent_link_permission(
            manga_id, row_version=version(form),
            allow_personalized=form.get("allow_personalized") == "yes",
        ), "torrent-link-permission-saved", with_config=True)

    @app.post(PREFIX + "/archives/{manga_id:path}/torrent-warnings")
    async def torrent_warnings(request: Request, manga_id: str):
        form = await _validated_form(request)
        return update(request, form, manga_id, lambda s: s.confirm_torrent_warnings(
            manga_id, row_version=version(form), warnings=list(form.getlist("warnings")),
            revoke=form.get("revoke") == "yes",
        ), "torrent-warning-updated", with_config=True)

    # ------------------------------------------------------------------ 进入特殊模块
    @app.post(PREFIX + "/special/start/{manga_id:path}")
    async def start_special(request: Request, manga_id: str):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                workflow = SpecialWorkflowService(
                    session, actor=_actor(request), config_dir=config_dir, app_config=app_config,
                ).start_for_manga(manga_id, row_version=version(form), load_options=True)
                kind, workflow_id = workflow.kind, workflow.id
        except (ValueError, SpecialServiceError) as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest("表单数据无效")
            return ctx.service_error(request, error)
        return redirect(request, f"{PREFIX}/workflows/{kind}/{workflow_id}", "workflow-created")

    @app.post(PREFIX + "/manual-torrent/start/{manga_id:path}")
    async def start_manual_torrent(request: Request, manga_id: str):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                workflow = ModuleService(
                    session, actor=_actor(request), config_dir=config_dir, app_config=app_config
                ).create("manual_torrent", {"manga_id": manga_id, "row_version": version(form)})
                kind, workflow_id = workflow.kind, workflow.id
        except ValueError as exc:
            error = exc if isinstance(exc, SpecialServiceError) else SpecialInvalidRequest(str(exc))
            return ctx.service_error(request, error)
        return redirect(request, f"{PREFIX}/workflows/{kind}/{workflow_id}", "workflow-created")
