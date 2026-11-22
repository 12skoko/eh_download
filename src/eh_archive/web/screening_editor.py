import json
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Annotated

import requests
from fastapi import Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from starlette.datastructures import FormData

from ..config import load_config
from ..db.models import MangaRecord
from ..domain.screen_rules import RuleTrace
from ..domain.states import Status
from ..services.collector.parser import EhTagTranslation
from ..services.screening import ScreeningPolicy
from .configuration import (
    ConfigurationConflict,
    ConfigurationError,
    load_screening_editor,
    save_screening_editor,
)
from .shared import _validated_form


def _tag_dictionary(ctx) -> bytes:
    for path in (ctx.config_dir / "db.text.json", Path("db.text.json")):
        if path.is_file():
            dictionary = EhTagTranslation(path)
            break
    else:
        crawl = load_config(ctx.config_dir)[2]
        network = ctx.secrets_config.network(ctx.app_config.browse_session)
        with requests.get(
            crawl.tag_translation_url,
            proxies=network.get("proxies"),
            timeout=ctx.supervisor_config.request_timeout_seconds,
        ) as response:
            response.raise_for_status()
            dictionary = EhTagTranslation(data=response.json())

    groups = {}
    legacy_names = {index: ns for ns, index in EhTagTranslation.rows.items()}
    for index, group in enumerate(dictionary.data["data"]):
        namespace = group.get("namespace", legacy_names.get(index))
        if namespace and namespace != "rows" and dictionary.values[index]:
            groups[namespace] = {
                "label": dictionary.group_names.get(namespace, namespace),
                "tags": dictionary.values[index],
            }
    if not groups:
        raise ValueError("词库中没有可用的标签")
    return JSONResponse(groups).body


def install_screening_editor(app, ctx):
    url = "/settings/crawl/screening"
    dictionary_lock = Lock()
    dictionary_bytes = None
    refresh_at = 0.0

    @app.get(url + "/tag-dictionary")
    def tag_dictionary():
        nonlocal dictionary_bytes, refresh_at
        with dictionary_lock:
            if monotonic() >= refresh_at:
                try:
                    dictionary_bytes = _tag_dictionary(ctx)
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    # Keep a usable dictionary when a refresh fails.
                    refresh_at = monotonic() + 30
                else:
                    refresh_at = monotonic() + 3600
            if dictionary_bytes is None:
                return JSONResponse({"detail": "翻译词库暂不可用"}, status_code=503)
            return Response(
                dictionary_bytes, media_type="application/json",
                headers={"Cache-Control": f"private, max-age={max(0, int(refresh_at - monotonic()))}"},
            )

    @app.get(url)
    def editor(request: Request):
        settings, revision = load_screening_editor(ctx.config_dir)
        trees = {name: json.loads(settings[name + "_json"]) for name in ("direct", "algorithm")}
        return ctx.page(request, "screening.html", trees=trees, revision=revision)

    @app.post(url + "/save")
    def save(form: Annotated[FormData, Depends(_validated_form)]):
        try:
            direct, algorithm = (str(form.get(key, "")) for key in ("direct", "algorithm"))
            ScreeningPolicy(direct, algorithm)
            revision = save_screening_editor(
                ctx.config_dir, direct, algorithm, str(form.get("revision", ""))
            )
            return {"revision": revision}
        except ConfigurationConflict as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except (ConfigurationError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.post(url + "/preview")
    def preview(form: Annotated[FormData, Depends(_validated_form)]):
        try:
            trace = RuleTrace()
            policy = ScreeningPolicy(
                str(form.get("direct", "")), str(form.get("algorithm", "")), trace=trace
            )
            limit = int(form.get("limit", 100))
            if not 1 <= limit <= 1000 or form.get("scope") not in {"all", "pending"}:
                raise ValueError("请选择有效的预览范围（1～1000 条）")
            query = select(MangaRecord).where(~MangaRecord.manga_id.like("picacg/%"))
            if form["scope"] == "pending":
                query = query.where(MangaRecord.status == Status.DISCOVERED.value)
            query = query.order_by(MangaRecord.created_at.desc(), MangaRecord.manga_id).limit(limit)
            counts = {"queue": 0, "compare_group": 0, "filter_out": 0}
            samples = {key: [] for key in counts}
            with ctx.database.session() as session:
                for row in session.scalars(query):
                    action = policy.evaluate(row).action.value
                    counts[action] += 1
                    samples[action].append({
                        "id": row.manga_id, "title": row.name, "trace": trace.take(),
                    })
            return {"counts": counts, "samples": samples}
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
