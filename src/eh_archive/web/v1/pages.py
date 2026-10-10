from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request

from ...db.models import SystemControl
from ...services.downloader.torrent.review import WARNING_LABELS as TORRENT_WARNING_LABELS
from ...special.service import (
    SpecialInvalidRequest,
    SpecialNotFound,
    SpecialServiceError,
    SpecialWorkflowService,
    active_special_workflow,
    special_entry_for_manga,
    special_module_health,
    special_workflow_detail,
)
from ...supervisor.scheduling import (
    COOLDOWN_MODULES,
    INTERVAL_MODULES,
    module_view,
    request_cooldown_release,
    request_run,
)
from ..auth import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS
from ..configuration import (
    POLICY_LABELS,
    ConfigurationConflict,
    ConfigurationError,
    load_config_sections,
    update_config_section,
)
from ..services import (
    InvalidRequest,
    WebService,
    WebServiceError,
    dashboard_data,
    list_events,
    list_manga,
    list_review_manga,
    manga_detail,
    manga_progress_data,
    review_facets,
    running_attempts,
    running_module_tasks,
)
from ..special_modules import get_special_module_page, special_module_cards, special_module_url

from fastapi import Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from ..shared import (
    _filter_query,
    _context,
    _validated_form,
    _actor,
    _optional_text,
    _optional_int,
    _safe_next,
    _redirect_response,
    _health_status,
    _supervisor_status,
    _manual_artifact_directories,
)
from .common import _page_update, _special_page_update, _special_error_response, _error_response


def install_v1(
    app,
    *,
    database,
    templates,
    app_config,
    supervisor_config,
    secrets_config,
    signer,
    auth_enabled,
    config_dir,
    management_config,
):
    status_query = Query(default=[])

    @app.get("/v1/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/v1/", error: str | None = None):
        if auth_enabled and signer and signer.verify(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse(_safe_next(next), status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="v1/login.html",
            context={"next": _safe_next(next), "error": error, "auth_enabled": auth_enabled},
        )

    @app.post("/v1/login")
    async def login(request: Request):
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        next_path = _safe_next(str(form.get("next", "/v1/")))
        from ..auth import verify_password

        valid = (
            auth_enabled
            and username == secrets_config.web_username
            and verify_password(password, secrets_config.web_password_hash)
        )
        if not valid:
            return templates.TemplateResponse(
                request=request,
                name="v1/login.html",
                context={"next": next_path, "error": "用户名或密码错误", "auth_enabled": True},
                status_code=401,
            )
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            signer.create(username),
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=False,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/v1/logout")
    async def logout(request: Request):
        await _validated_form(request)
        response = _redirect_response(request, "/v1/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/v1/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with database.session() as session:
            data = dashboard_data(session)
        health_states = {
            component: _health_status(item, supervisor_config.health_check_interval_seconds)
            for component, item in data["health"].items()
        }
        supervisor_state = _supervisor_status(
            data["controls"].get("supervisor"), supervisor_config.poll_seconds
        )
        return templates.TemplateResponse(
            request=request,
            name="v1/dashboard.html",
            context=_context(
                request,
                **data,
                health_states=health_states,
                supervisor_state=supervisor_state,
                module_schedules={
                    name: module_view(
                        name,
                        data["controls"].get(name),
                        data["controls"].get("supervisor"),
                        supervisor_config,
                        app_config.timezone,
                    )
                    for name in COOLDOWN_MODULES
                },
            ),
        )

    def module_schedule_data(component):
        with database.session() as session:
            return module_view(
                component,
                session.get(SystemControl, component),
                session.get(SystemControl, "supervisor"),
                supervisor_config,
                app_config.timezone,
            )

    def module_schedule_response(request, component, error=None):
        return templates.TemplateResponse(
            request=request,
            name="v1/_module_schedule.html",
            context=_context(
                request,
                component=component,
                schedule=module_schedule_data(component),
                trigger_error=error,
                schedule_oob=True,
            ),
        )

    @app.get("/v1/partials/module-schedule/{component}", response_class=HTMLResponse)
    def module_schedule_partial(request: Request, component: str):
        if component not in COOLDOWN_MODULES:
            return _error_response(request, templates, InvalidRequest("未知模块"))
        return module_schedule_response(request, component)

    @app.post("/v1/control/{component}/release-cooldown")
    async def release_cooldown_page(request: Request, component: str):
        form = await _validated_form(request)
        try:
            if form.get("confirmed") != "yes":
                raise ValueError("请先确认解除冷却")
            with database.session() as session:
                request_cooldown_release(
                    session,
                    component,
                    owner=str(form.get("owner", "")),
                    version=str(form.get("cooldown_version", "")),
                    actor=_actor(request),
                    config=supervisor_config,
                    timezone=app_config.timezone,
                )
        except ValueError as exc:
            if request.headers.get("HX-Request") == "true" and component in COOLDOWN_MODULES:
                return module_schedule_response(request, component, str(exc))
            return _error_response(request, templates, InvalidRequest(str(exc)))
        if request.headers.get("HX-Request") == "true":
            return module_schedule_response(request, component)
        return _redirect_response(request, "/v1/?notice=cooldown-release-requested")

    @app.post("/v1/control/{component}/run")
    async def run_module_page(request: Request, component: str):
        form = await _validated_form(request)
        try:
            if form.get("confirmed") != "yes":
                raise ValueError("请先确认手动运行")
            with database.session() as session:
                request_run(
                    session,
                    component,
                    owner=str(form.get("owner", "")),
                    request_version=str(form.get("request_version", "")),
                    actor=_actor(request),
                    config=supervisor_config,
                    timezone=app_config.timezone,
                )
        except ValueError as exc:
            if request.headers.get("HX-Request") == "true" and component in INTERVAL_MODULES:
                return module_schedule_response(request, component, str(exc))
            return _error_response(request, templates, InvalidRequest(str(exc)))
        if request.headers.get("HX-Request") == "true":
            return module_schedule_response(request, component)
        return _redirect_response(request, "/v1/?notice=module-trigger-requested")

    @app.get("/v1/partials/running-tasks", response_class=HTMLResponse)
    def running_tasks_partial(request: Request):
        with database.session() as session:
            running = running_attempts(session)
            running_modules = running_module_tasks(session)
        return templates.TemplateResponse(
            request=request,
            name="v1/_running_tasks.html",
            context=_context(request, running=running, running_module_tasks=running_modules),
        )

    @app.get("/v1/partials/manga-progress/{manga_id:path}", response_class=HTMLResponse)
    def manga_progress_partial(request: Request, manga_id: str):
        try:
            with database.session() as session:
                progress = manga_progress_data(session, manga_id)
        except WebServiceError as exc:
            return _error_response(request, templates, exc)
        return templates.TemplateResponse(
            request=request,
            name="v1/_direct_download_progress.html",
            context=_context(request, **progress),
        )

    @app.get("/v1/manga", response_class=HTMLResponse)
    def manga_queue(
        request: Request,
        status: list[str] = status_query,
        q: str | None = None,
        uploader: str | None = None,
        tags: str | None = None,
        queue_source: str | None = None,
        has_error: str | None = None,
        limit: int = 50,
        page: int = 1,
    ):
        error_filter = None if has_error not in {"yes", "no"} else has_error == "yes"
        try:
            with database.session() as session:
                page = list_manga(
                    session,
                    statuses=status,
                    query_text=q,
                    uploader=uploader,
                    tags=tags,
                    queue_source=queue_source,
                    has_error=error_filter,
                    limit=limit,
                    page=page,
                )
        except WebServiceError as exc:
            return _error_response(request, templates, exc)
        filter_params = []
        if q:
            filter_params.append(("q", q))
        if uploader:
            filter_params.append(("uploader", uploader))
        if tags:
            filter_params.append(("tags", tags))
        if queue_source:
            filter_params.append(("queue_source", queue_source))
        if has_error:
            filter_params.append(("has_error", has_error))
        filter_params.append(("limit", str(limit)))
        filter_params.extend(("status", s) for s in status)
        return templates.TemplateResponse(
            request=request,
            name=(
                "v1/_manga_results.html"
                if request.headers.get("HX-Target") == "manga-results"
                else "v1/manga/list.html"
            ),
            context=_context(
                request,
                page=page,
                selected_statuses=status,
                q=q or "",
                uploader=uploader or "",
                tags=tags or "",
                queue_source=queue_source or "",
                has_error=has_error or "",
                limit=limit,
                filter_qs=_filter_query(filter_params),
            ),
        )

    @app.get("/v1/review", response_class=HTMLResponse)
    def review_page(
        request: Request,
        status: str = "manual_review",
        q: str | None = None,
        error_code: str | None = None,
        operation: str | None = None,
        page: int = 1,
    ):
        if status not in {"manual_review", "quarantined"}:
            status = "manual_review"
        try:
            with database.session() as session:
                page = list_review_manga(
                    session,
                    status=status,
                    query_text=q,
                    error_code=error_code,
                    operation=operation,
                    limit=50,
                    page=page,
                )
                error_facets, operations = review_facets(
                    session,
                    status=status,
                    query_text=q,
                    operation=operation,
                )
        except WebServiceError as exc:
            return _error_response(request, templates, exc)
        filter_params = [("status", status)]
        if q:
            filter_params.append(("q", q))
        if operation:
            filter_params.append(("operation", operation))
        if error_code:
            filter_params.append(("error_code", error_code))
        return templates.TemplateResponse(
            request=request,
            name=(
                "v1/_review_workspace.html"
                if request.headers.get("HX-Target") == "review-workspace"
                else "v1/review.html"
            ),
            context=_context(
                request,
                page=page,
                selected_status=status,
                q=q or "",
                error_code=error_code or "",
                operation=operation or "",
                error_facets=error_facets,
                review_operations=operations,
                filter_qs=_filter_query(filter_params),
            ),
        )

    @app.get("/v1/events", response_class=HTMLResponse)
    def events_page(
        request: Request,
        manga_id: str | None = None,
        component: str | None = None,
        operation: str | None = None,
        error_only: bool = False,
        limit: int = 100,
        page: int = 1,
    ):
        with database.session() as session:
            events_page = list_events(
                session,
                manga_id=manga_id,
                component=component,
                operation=operation,
                error_only=error_only,
                limit=limit,
                page=page,
            )
        filter_params = []
        if manga_id:
            filter_params.append(("manga_id", manga_id))
        if component:
            filter_params.append(("component", component))
        if operation:
            filter_params.append(("operation", operation))
        if error_only:
            filter_params.append(("error_only", "true"))
        filter_params.append(("limit", str(limit)))
        return templates.TemplateResponse(
            request=request,
            name="v1/events.html",
            context=_context(
                request,
                page=events_page,
                manga_id=manga_id or "",
                component=component or "",
                operation=operation or "",
                error_only=error_only,
                limit=limit,
                filter_qs=_filter_query(filter_params),
            ),
        )

    @app.get("/v1/config", response_class=HTMLResponse)
    def config_page(
        request: Request,
        section: str = "app",
        saved: str | None = None,
    ):
        return render_config(request, section, saved=saved)

    def render_config(request, selected, *, saved=None, error=None, form=None, status=200):
        from dataclasses import replace

        sections = load_config_sections(config_dir)
        active = next((s for s in sections if s.name == selected), sections[0])
        if isinstance(error, ConfigurationConflict) and form is not None:
            active = replace(active, revision=str(form.get("revision", "")))
        if form is not None:
            active = replace(
                active,
                fields=tuple(
                    replace(
                        field,
                        value=str(form.get(field.name, field.value)) if not field.secret else "",
                        checked=field.name in form if field.editable else field.checked,
                        error=error.fields.get(field.name, "") if error else "",
                    )
                    for field in active.fields
                ),
            )
        messages = {
            "next_worker": "已保存。下一次 Worker 启动时生效，当前任务继续使用原配置。",
            "supervisor": "已保存。需要重启 Supervisor 后生效。",
            "web": "已保存。需要重启 Web 后生效。",
            "web_and_supervisor": "已保存。需要重启 Web 和 Supervisor 后生效。",
            "none": "配置没有变化。",
        }
        return templates.TemplateResponse(
            request=request,
            name="v1/config.html",
            status_code=status,
            context=_context(
                request,
                sections=sections,
                active_section=active,
                config_error=str(error) if error else active.error,
                notice=messages.get(saved),
                saved_policy=saved,
                policy_labels=POLICY_LABELS,
            ),
        )

    @app.post("/v1/config/{section_name}")
    async def update_config_page(request: Request, section_name: str):
        form = await _validated_form(request)
        try:
            result = update_config_section(
                config_dir,
                section_name,
                form,
                revision=str(form.get("revision", "")),
            )
        except ConfigurationError as exc:
            status = 409 if isinstance(exc, ConfigurationConflict) else 422
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": str(exc), "fields": exc.fields}, status_code=status)
            return render_config(request, section_name, error=exc, form=form, status=status)
        target = "/v1/config?" + urlencode(
            {
                "section": section_name,
                "saved": result.restart if result.changed_fields else "none",
            }
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {
                    "redirect": target,
                    "changed_fields": result.changed_fields,
                    "policy": result.restart,
                }
            )
        return _redirect_response(request, target)

    from .special_routes import install_special_routes

    install_special_routes(app, database, templates, app_config, config_dir)

    @app.get("/v1/special", response_class=HTMLResponse)
    def special_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="v1/special.html",
            context=_context(
                request,
                modules=tuple(
                    {**card, "url": "/v1" + card["url"]}
                    for card in special_module_cards(config_dir)
                ),
            ),
        )

    @app.get("/v1/special/modules/{kind}", response_class=HTMLResponse)
    def special_module_page(
        request: Request,
        kind: str,
        page: int = 1,
        notice: str | None = None,
        found: int | None = None,
        queued: int | None = None,
        skipped: int | None = None,
    ):
        try:
            module = get_special_module_page(kind)
        except ValueError:
            return _special_error_response(
                request,
                templates,
                SpecialNotFound("特殊处理模块不存在"),
            )
        with database.session() as session:
            data = module.load_dashboard(session, page=max(1, page))
        if kind == "full_collect":
            from ...special.modules.full_collect.module import creation_defaults

            data.update(creation_defaults(config_dir))
        module_health = special_module_health(module.kind, config_dir)
        return templates.TemplateResponse(
            request=request,
            name="v1/" + module.template_name,
            context=_context(
                request,
                **data,
                module=module,
                module_url="/v1" + module.url,
                module_health=module_health,
                notice=notice,
                batch_found=found,
                batch_queued=queued,
                batch_skipped=skipped,
            ),
        )

    @app.post("/v1/special/video-archive/collect-ready")
    async def collect_ready_page(request: Request):
        await _validated_form(request)
        try:
            with database.session() as session:
                result = SpecialWorkflowService(
                    session,
                    actor=_actor(request),
                    config_dir=config_dir,
                    app_config=app_config,
                ).dispatch_ready_checks()
        except SpecialServiceError as exc:
            return _special_error_response(request, templates, exc)
        return _redirect_response(
            request,
            "/v1/special/modules/video_archive?notice=batch-dispatched"
            f"&found={result.found}&queued={result.queued}&skipped={result.skipped}",
        )

    @app.post("/v1/special/video-archive/cleanup-completed")
    async def cleanup_completed_page(request: Request):
        await _validated_form(request)
        try:
            with database.session() as session:
                result = SpecialWorkflowService(
                    session,
                    actor=_actor(request),
                    config_dir=config_dir,
                    app_config=app_config,
                ).dispatch_source_cleanups()
        except SpecialServiceError as exc:
            return _special_error_response(request, templates, exc)
        return _redirect_response(
            request,
            "/v1/special/modules/video_archive?notice=cleanup-dispatched"
            f"&found={result.found}&queued={result.queued}&skipped={result.skipped}",
        )

    @app.post("/v1/special/start/{manga_id:path}")
    async def start_special_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        try:
            with database.session() as session:
                workflow = SpecialWorkflowService(
                    session,
                    actor=_actor(request),
                    config_dir=config_dir,
                    app_config=app_config,
                ).start_for_manga(
                    manga_id,
                    row_version=int(str(form.get("row_version", ""))),
                    load_options=True,
                )
        except (ValueError, SpecialServiceError) as exc:
            error = (
                exc
                if isinstance(exc, SpecialServiceError)
                else SpecialInvalidRequest("表单数据无效")
            )
            return _special_error_response(request, templates, error)
        return _redirect_response(request, f"/v1/special/workflows/{workflow.id}")

    @app.get("/v1/special/workflows/{workflow_id}", response_class=HTMLResponse)
    def special_workflow_page(
        request: Request, workflow_id: int, notice: str | None = None, page: int = 1
    ):
        try:
            with database.session() as session:
                detail = special_workflow_detail(session, workflow_id, page=page)
            module_health = special_module_health(detail["workflow"].kind, config_dir)
        except SpecialServiceError as exc:
            return _special_error_response(request, templates, exc)
        return templates.TemplateResponse(
            request=request,
            name="v1/" + detail["detail_template"],
            context=_context(
                request,
                **detail,
                notice=notice,
                module_health=module_health,
                module_url="/v1" + special_module_url(detail["workflow"].kind),
            ),
        )

    @app.get(
        "/v1/partials/special-workflows/{workflow_id}",
        response_class=HTMLResponse,
    )
    def special_workflow_partial(request: Request, workflow_id: int):
        try:
            with database.session() as session:
                detail = special_workflow_detail(session, workflow_id)
            # Only declarative module configuration is read here.  Dependency
            # probes belong to the one-shot operation that needs them.
            module_health = special_module_health(detail["workflow"].kind, config_dir)
        except SpecialServiceError as exc:
            return _special_error_response(request, templates, exc)
        return templates.TemplateResponse(
            request=request,
            name="v1/" + detail["panel_template"],
            context=_context(
                request,
                **detail,
                notice=None,
                module_health=module_health,
                module_url="/v1" + special_module_url(detail["workflow"].kind),
            ),
        )

    @app.post("/v1/special/workflows/{workflow_id}/load")
    async def special_load_page(request: Request, workflow_id: int):
        form = await _validated_form(request)
        return _special_page_update(
            request,
            templates,
            database,
            workflow_id,
            lambda service: service.queue_load(
                workflow_id, row_version=int(str(form.get("row_version", "")))
            ),
            config_dir=config_dir,
            app_config=app_config,
            notice="load-queued",
        )

    @app.post("/v1/special/workflows/{workflow_id}/select")
    async def special_select_page(request: Request, workflow_id: int):
        form = await _validated_form(request)
        return _special_page_update(
            request,
            templates,
            database,
            workflow_id,
            lambda service: service.select_torrents(
                workflow_id,
                row_version=int(str(form.get("row_version", ""))),
                image_choice_id=str(form.get("image_choice_id", "")),
                video_choice_id=str(form.get("video_choice_id", "")),
                confirmed_warnings=form.getlist("confirmed_warnings"),
            ),
            config_dir=config_dir,
            app_config=app_config,
            notice="selection-queued",
        )

    @app.post("/v1/special/workflows/{workflow_id}/check")
    async def special_check_page(request: Request, workflow_id: int):
        form = await _validated_form(request)
        return _special_page_update(
            request,
            templates,
            database,
            workflow_id,
            lambda service: service.queue_check(
                workflow_id, row_version=int(str(form.get("row_version", "")))
            ),
            config_dir=config_dir,
            app_config=app_config,
            notice="check-queued",
        )

    @app.post("/v1/special/workflows/{workflow_id}/cleanup-sources")
    async def special_cleanup_sources_page(request: Request, workflow_id: int):
        form = await _validated_form(request)
        return _special_page_update(
            request,
            templates,
            database,
            workflow_id,
            lambda service: service.queue_source_cleanup(
                workflow_id, row_version=int(str(form.get("row_version", "")))
            ),
            config_dir=config_dir,
            app_config=app_config,
            notice="cleanup-queued",
        )

    @app.post("/v1/special/workflows/{workflow_id}/retry")
    async def special_retry_page(request: Request, workflow_id: int):
        form = await _validated_form(request)
        return _special_page_update(
            request,
            templates,
            database,
            workflow_id,
            lambda service: service.retry(
                workflow_id, row_version=int(str(form.get("row_version", "")))
            ),
            config_dir=config_dir,
            app_config=app_config,
            notice="retry-queued",
        )

    @app.post("/v1/special/workflows/{workflow_id}/cancel")
    async def special_cancel_page(request: Request, workflow_id: int):
        form = await _validated_form(request)
        return _special_page_update(
            request,
            templates,
            database,
            workflow_id,
            lambda service: service.cancel(
                workflow_id, row_version=int(str(form.get("row_version", "")))
            ),
            config_dir=config_dir,
            app_config=app_config,
            notice="cancel-queued",
        )

    @app.post("/v1/special/workflows/{workflow_id}/lease/release-expired")
    async def special_release_lease_page(request: Request, workflow_id: int):
        form = await _validated_form(request)
        return _special_page_update(
            request,
            templates,
            database,
            workflow_id,
            lambda service: service.release_expired_job(
                workflow_id,
                row_version=int(str(form.get("row_version", ""))),
                reason=str(form.get("reason", "")),
                confirmed=form.get("confirmed") == "yes",
            ),
            config_dir=config_dir,
            app_config=app_config,
            notice="lease-released",
        )

    @app.post("/v1/special/workflows/{workflow_id}/exit-without-cleanup")
    async def special_exit_without_cleanup_page(request: Request, workflow_id: int):
        form = await _validated_form(request)
        return _special_page_update(
            request,
            templates,
            database,
            workflow_id,
            lambda service: service.exit_without_cleanup(
                workflow_id,
                row_version=int(str(form.get("row_version", ""))),
                reason=str(form.get("reason", "")),
                confirmed=form.get("confirmed") == "yes",
            ),
            config_dir=config_dir,
            app_config=app_config,
            notice="workflow-exited",
        )

    @app.get("/v1/manga/{manga_id:path}", response_class=HTMLResponse)
    def manga_page(request: Request, manga_id: str, notice: str | None = None):
        try:
            with database.session() as session:
                detail = manga_detail(session, manga_id)
                row = detail["row"]
                workflow = (
                    active_special_workflow(session, row.manga_id)
                    if row.status == "special_processing"
                    else None
                )
        except WebServiceError as exc:
            return _error_response(request, templates, exc)
        entry = special_entry_for_manga(row, config_dir)
        return templates.TemplateResponse(
            request=request,
            name="v1/manga/detail.html",
            context=_context(
                request,
                **detail,
                notice=notice,
                special_entry=entry,
                special_workflow=workflow,
                torrent_warning_labels=TORRENT_WARNING_LABELS,
                artifact_directories=_manual_artifact_directories(app_config, manga_id),
            ),
        )

    @app.post("/v1/manga/{manga_id:path}/remark")
    async def update_remark_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.update_remark(
                manga_id,
                remark=_optional_text(form.get("remark")),
                row_version=int(str(form.get("row_version", ""))),
            ),
            "remark-updated",
        )

    @app.post("/v1/manga/{manga_id:path}/priority")
    async def update_priority_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.update_priority(
                manga_id,
                priority=int(str(form.get("priority", ""))),
                row_version=int(str(form.get("row_version", ""))),
            ),
            "priority-updated",
        )

    @app.post("/v1/manga/{manga_id:path}/skip-video")
    async def skip_video_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.skip_video_and_resume(
                manga_id,
                row_version=int(str(form.get("row_version", ""))),
            ),
            "video-skipped",
        )

    @app.post("/v1/manga/{manga_id:path}/actions/{action}")
    async def manga_action_page(request: Request, manga_id: str, action: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.action(
                manga_id,
                action,
                row_version=int(str(form.get("row_version", ""))),
                reason=_optional_text(form.get("reason")),
                archive_id=_optional_text(form.get("archive_id")),
            ),
            "action-completed",
        )

    @app.post("/v1/manga/{manga_id:path}/lease/release-expired")
    async def release_expired_lease_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.release_expired_lease(
                manga_id,
                row_version=int(str(form.get("row_version", ""))),
                reason=_optional_text(form.get("reason")),
                confirmed=form.get("confirmed") == "yes",
            ),
            "expired-lease-released",
        )

    @app.post("/v1/manga/{manga_id:path}/conflict-versions")
    async def conflict_versions_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.resolve_conflict_versions(
                manga_id,
                row_version=int(str(form.get("row_version", ""))),
                old_versions={
                    str(key): int(str(form.get(f"version:{key}", "")))
                    for key in form.getlist("old_ids")
                },
                target_status=str(form.get("target_status", "")),
                reason=_optional_text(form.get("reason")),
                confirmed=form.get("confirmed") == "yes",
            ),
            "conflict-versions-resolved",
            app_config=app_config,
        )

    @app.post("/v1/manga/{manga_id:path}/conflict-rename")
    async def conflict_rename_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.request_conflict_rename(
                manga_id,
                row_version=int(str(form.get("row_version", ""))),
                target_filename=_optional_text(form.get("target_filename")),
                reason=_optional_text(form.get("reason")),
                confirmed=form.get("confirmed") == "yes",
            ),
            "conflict-rename-requested",
            app_config=app_config,
        )

    @app.post("/v1/manga/{manga_id:path}/status/{target_status}")
    async def override_manga_status_page(request: Request, manga_id: str, target_status: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.override_status(
                manga_id,
                target_status=target_status,
                row_version=int(str(form.get("row_version", ""))),
                reason=_optional_text(form.get("reason")),
                download_method=_optional_text(form.get("download_method")),
                artifact_filename=_optional_text(form.get("artifact_filename")),
                archive_id=_optional_text(form.get("archive_id")),
                superseded_by_id=_optional_text(form.get("superseded_by_id")),
                confirmation_manga_id=_optional_text(form.get("confirmation_manga_id")),
                allow_web_only=True,
                config_dir=config_dir,
            ),
            "status-updated",
            app_config=app_config,
        )

    @app.post("/v1/manga/{manga_id:path}/torrent-link-permission")
    async def torrent_link_permission_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.set_torrent_link_permission(
                manga_id,
                row_version=int(str(form.get("row_version", ""))),
                allow_personalized=form.get("allow_personalized") == "yes",
            ),
            "torrent-link-permission-saved",
            app_config=app_config,
        )

    @app.post("/v1/manga/{manga_id:path}/torrent-warnings")
    async def torrent_warnings_page(request: Request, manga_id: str):
        form = await _validated_form(request)
        return _page_update(
            request,
            templates,
            database,
            manga_id,
            lambda service: service.confirm_torrent_warnings(
                manga_id,
                row_version=int(str(form.get("row_version", ""))),
                warnings=list(form.getlist("warnings")),
                revoke=form.get("revoke") == "yes",
            ),
            "torrent-warning-updated",
            app_config=app_config,
        )

    @app.post("/v1/control/{component}")
    async def control_page(request: Request, component: str):
        form = await _validated_form(request)
        from ...management.service import control_guard

        try:
            with control_guard(component, Path(management_config)), database.session() as session:
                control = WebService(session, actor=_actor(request)).set_control(
                    component,
                    state=str(form.get("state", "")),
                    reason=_optional_text(form.get("reason")),
                    row_version=_optional_int(form.get("row_version")),
                )
        except (ValueError, WebServiceError) as exc:
            error = exc if isinstance(exc, WebServiceError) else InvalidRequest("表单数据无效")
            if request.headers.get("HX-Request") == "true":
                with database.session() as session:
                    current = session.get(SystemControl, component)
                row_template = (
                    "v1/_supervisor_row.html"
                    if component == "supervisor"
                    else "v1/_component_row.html"
                )
                return templates.TemplateResponse(
                    request=request,
                    name=row_template,
                    context=_context(
                        request,
                        component=component,
                        control=current,
                        supervisor_state=(
                            _supervisor_status(current, supervisor_config.poll_seconds)
                            if component == "supervisor"
                            else None
                        ),
                        component_error=str(error),
                        module_schedules={component: module_schedule_data(component)}
                        if component in COOLDOWN_MODULES
                        else {},
                    ),
                )
            return _error_response(request, templates, error)
        if request.headers.get("HX-Request") == "true":
            row_template = (
                "v1/_supervisor_row.html" if component == "supervisor" else "v1/_component_row.html"
            )
            return templates.TemplateResponse(
                request=request,
                name=row_template,
                context=_context(
                    request,
                    component=component,
                    control=control,
                    supervisor_state=(
                        _supervisor_status(control, supervisor_config.poll_seconds)
                        if component == "supervisor"
                        else None
                    ),
                    component_error=None,
                    module_schedules={component: module_schedule_data(component)}
                    if component in COOLDOWN_MODULES
                    else {},
                ),
            )
        return _redirect_response(request, "/v1/?notice=control-updated")

    from .logs import register as register_logs

    register_logs(app, templates, _context, app_config.log_dir)

    from .management import register as register_management

    register_management(app, templates, _context, database, Path(management_config))
