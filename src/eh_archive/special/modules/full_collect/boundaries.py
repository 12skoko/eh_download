"""Frozen scope and runtime-verified pagination; no network or scheduling here."""

import hashlib
import json
from datetime import UTC, timedelta
from itertools import pairwise
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from ....db.models import MangaRecord
from ....db.repository import utcnow
from .config import parse_start_at, validate_listing_url


def listing_url(base, **cursor):
    parts = urlsplit(validate_listing_url(base))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for name in ("next", "prev", "seek"):
        query.pop(name, None)
    # An account's hidden-language/uploader/tag preferences must not narrow scope.
    query.update(f_cats="0", f_sfl="on", f_sfu="on", f_sft="on", inline_set="dm_e")
    query.update({name: str(value) for name, value in cursor.items()})
    return validate_listing_url(urlunsplit((parts.scheme, parts.netloc, "/", urlencode(query), "")))


def scope_key(base, account):
    return hashlib.sha256(
        json.dumps(
            {"base": listing_url(base), "account": account, "order": "gid-desc-v1"},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def initial_scope(session, config, app, inputs, *, backfill=False):
    allowed = {"start_mode", "start_at", "start_url"}
    if set(inputs) - allowed:
        raise ValueError("未知起点参数")
    mode = inputs.get("start_mode", config.start_mode)
    if mode not in {"date", "database", "url"}:
        raise ValueError("起点模式必须为 date/database/url")
    days = config.backfill_default_days_ago if backfill else config.start_days_ago
    supplied = inputs.get("start_at", "" if backfill else config.start_at)
    upper = parse_start_at(supplied) or (utcnow() - timedelta(days=days))
    if upper >= utcnow():
        raise ValueError("起点必须早于当前时间")
    base = listing_url(config.base_url)
    anchor_id = None
    if mode == "url":
        url = inputs.get("start_url", config.start_url)
        url = validate_listing_url(url, base_url=base)
        query = dict(parse_qsl(urlsplit(url).query))
        if not any(name in query for name in ("next", "prev", "seek")):
            raise ValueError("手工起点须包含分页游标，不能从最新页开始")
        url = listing_url(url, **{k: v for k, v in query.items() if k in {"next", "prev", "seek"}})
    else:
        target = upper
        if mode == "database":
            # This record is only a navigation hint, never proof of page coverage.
            record = session.scalar(
                select(MangaRecord)
                .where(
                    MangaRecord.posted_at <= upper,
                    MangaRecord.link.like(f"https://{urlsplit(base).netloc}/g/%"),
                    ~MangaRecord.manga_id.like("picacg/%"),
                )
                .order_by(MangaRecord.posted_at.desc(), MangaRecord.manga_id.desc())
                .limit(1)
            )
            if record is None:
                raise ValueError("数据库没有目标时间之前的同站点档案，请选择日期或手工 URL")
            target = (
                record.posted_at.replace(tzinfo=UTC)
                if record.posted_at.tzinfo is None
                else record.posted_at
            )
            anchor_id = record.manga_id
        # Seek is only an initial hint. The executor follows the site's previous
        # links until it observes the upper boundary or the explicit first page.
        url = listing_url(
            base, seek=(target.astimezone(UTC) + timedelta(days=1)).date().isoformat()
        )
    return {
        "mode": mode,
        "upper_at": upper.isoformat(),
        "initial_url": url,
        "base_url": base,
        "account": app.full_collect_session.account,
        "fingerprint": scope_key(base, app.full_collect_session.account),
        "database_anchor": anchor_id,
    }


def page_ids(page):
    ids = [int(item.manga_id.split("/", 1)[0]) for item in page.items]
    if any(left <= right for left, right in pairwise(ids)):
        raise ValueError("列表未按 GID 严格降序排列，无法确认分页边界")
    return ids


def deadline(payload, now=None):
    now = now or utcnow()
    return max(
        [now]
        + [
            parsed
            for key in ("next_request_at", "cooldown_until", "batch_not_before")
            if (parsed := parse_start_at(payload.get(key, "")))
        ]
    )
