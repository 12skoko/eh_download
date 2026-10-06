"""Frozen ID intervals and runtime-verified pagination; no network or scheduling here."""

import hashlib
import json
from itertools import pairwise
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ....config.loader import SessionRole
from ....db.repository import utcnow
from .config import parse_start_at, validate_base_url, validate_listing_url


def listing_url(base, **cursor):
    parts = urlsplit(validate_listing_url(base))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for name in ("next", "prev", "seek", "inline_set"):
        query.pop(name, None)
    query.update(f_cats="0", f_sfl="on", f_sfu="on", f_sft="on")
    query.update({name: str(value) for name, value in cursor.items()})
    return validate_listing_url(urlunsplit((parts.scheme, parts.netloc, "/", urlencode(query), "")))


def id_range(inputs):
    result = []
    for name, minimum in (("start_id", 1), ("end_id", 0)):
        value = inputs.get(name)
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            value = int(value)
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} 必须是至少为 {minimum} 的整数")
        result.append(value)
    start, end = result
    if end >= start:
        raise ValueError("结束 ID 必须小于起始 ID；0 表示采集到站点末页")
    return start, end


def scope_key(base, account, start_id, end_id):
    # Display parameters and query ordering do not change the interval identity.
    parts = urlsplit(listing_url(base))
    return hashlib.sha256(json.dumps({
        "host": parts.netloc, "account": account,
        "filters": {"f_cats": "0", "f_sfl": "on", "f_sfu": "on", "f_sft": "on"},
        "order": "gid-desc-v1", "start_id": start_id, "end_id": end_id,
    }, sort_keys=True).encode()).hexdigest()


def initial_scope(config, app, secrets, inputs):
    if set(inputs) - {"base_url", "account", "start_id", "end_id"}:
        raise ValueError("未知采集范围参数")
    start, end = id_range(inputs)
    base = listing_url(validate_base_url(inputs.get("base_url", config.base_url)))
    account = inputs.get("account", app.full_collect_session.account)
    if not isinstance(account, str) or not account or account not in secrets.accounts:
        raise ValueError("请选择已配置的全量采集账号")
    role = SessionRole(account, app.full_collect_session.network)
    if not secrets.cookies(role):
        raise ValueError("全量采集账号必须配置 Cookie")
    secrets.proxy_pool(role)
    return {
        "start_id": start, "end_id": end, "base_url": base, "account": account,
        "initial_url": listing_url(base, next=start),
        "fingerprint": scope_key(base, account, start, end),
    }


def page_ids(page):
    ids = [int(item.manga_id.split("/", 1)[0]) for item in page.items]
    if any(left <= right for left, right in pairwise(ids)):
        raise ValueError("列表未按 GID 严格降序排列，无法确认分页边界")
    return ids


def deadline(payload, now=None):
    now = now or utcnow()
    return max([now] + [
        parsed for key in ("next_request_at", "cooldown_until", "batch_not_before")
        if (parsed := parse_start_at(payload.get(key, "")))
    ])
