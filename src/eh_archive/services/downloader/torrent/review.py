"""Torrent warnings and explicit, snapshot-scoped user decisions."""

import hashlib
import json

from requests import RequestException

from ....domain.errors import ArchiveError, ErrorClass
from .core import _parse_size, parse_torrent_options, select_torrent

WARNING_LABELS = {
    "video_torrent": "存在视频种子",
    "torrent_size_too_small": "种子小于预计大小的 60%",
    "torrent_personalized_link_required": "普通链接不存在，需要授权尝试个性化链接",
}


class TorrentReviewRequired(ArchiveError):
    def __init__(self, review):
        self.review = review
        missing = set(review["warnings"]) - set(review.get("accepted_warnings", []))
        code = next((w for w in review["warnings"] if w in missing), review["warnings"][0])
        super().__init__(code, "；".join(WARNING_LABELS[w] for w in missing), ErrorClass.ITEM)


def candidate_snapshot(option, estimated):
    result = option.public_snapshot()
    result["warnings"] = (["video_torrent"] if option.video else []) + (
        ["torrent_size_too_small"] if option.size_bytes * 5 < estimated * 3 else []
    )
    result["blocked"] = [
        name
        for name, blocked in (
            ("过时种子", option.outdated),
            ("重采样种子", option.resampled),
            ("没有 Seeder", option.seeds == 0),
        )
        if blocked
    ]
    result["size_percent"] = round(option.size_bytes * 100 / estimated, 1)
    result["has_personalized_link"] = bool(option.personalized_url)
    return result


def choose_with_review(html, *, estimated_size_raw, skip_video=False, review=None, **options):
    candidates = parse_torrent_options(html, include_outdated=False, bind_download_url=True, **options)
    expected = _parse_size(estimated_size_raw, field="estimated")
    # Dynamic seed counts do not invalidate acknowledgements; identities and sizes do.
    scope = hashlib.sha256(
        json.dumps(
            [expected, sorted((c.choice_id, c.outdated, c.resampled, c.video) for c in candidates)],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    previous = review or {}
    accepted = list(previous.get("accepted_warnings", [])) if previous.get("scope") == scope else []
    active = [c for c in candidates if not c.outdated]
    usable = [c for c in active if not c.resampled]
    warnings = []
    if any(c.video for c in active) and not skip_video:
        warnings.append("video_torrent")
    if usable and all(c.size_bytes * 5 < expected * 3 for c in usable):
        warnings.append("torrent_size_too_small")
    decision = {
        "scope": scope,
        "warnings": warnings,
        "accepted_warnings": accepted,
        "choices": [candidate_snapshot(c, expected) for c in candidates],
    }
    # Preserve the original ordering policy. Collect all soft warnings before raising.
    try:
        choice = select_torrent(
            html,
            estimated_size_raw=estimated_size_raw,
            skip_video=True,
            skip_small="torrent_size_too_small" in warnings,
            **options,
        )
    except ArchiveError as exc:
        if exc.info.code == "latest_torrent_no_seeder":
            raise
        if set(warnings) - set(accepted):
            raise TorrentReviewRequired(decision) from None
        raise
    decision["choice_id"] = next(c.choice_id for c in candidates if c.url == choice.url)
    if previous.get("choice_id") != decision["choice_id"]:
        decision["accepted_warnings"] = [
            w for w in accepted if w != "torrent_personalized_link_required"
        ]
    if previous.get("allow_personalized_next_attempt") is True:
        # Bind the one-shot permission to this actual candidate; later candidates
        # still need their own acknowledgement. This never bypasses other warnings.
        decision["accepted_warnings"] = sorted(set(decision["accepted_warnings"]) | {
            "torrent_personalized_link_required",
        })
    if set(warnings) - set(accepted):
        raise TorrentReviewRequired(decision)
    return choice, decision


def download_torrent(http, choice, *, decision, request_options=None, manual_fallback=None):
    request_options = request_options or {}

    def fetch(url):
        # Do not propagate URLs containing account tokens in exception messages.
        try:
            response = http.get(url, **request_options)
            missing = b"The torrent file could not be found" in response.content[:2048]
            if not missing:
                response.raise_for_status()
            return bytes(response.content), missing
        except ArchiveError as exc:
            raise ArchiveError(
                exc.info.code,
                "种子文件请求失败，请检查站点、网络或登录状态",
                exc.info.category,
                retryable=exc.info.retryable,
            ) from None
        except RequestException:
            raise ArchiveError(
                "torrent_fetch_failed", "种子文件请求失败，请检查网络或登录状态", ErrorClass.ITEM
            ) from None

    if manual_fallback is not None:
        def fetch_validated(url):
            content, missing = fetch(url)
            if missing:
                raise ArchiveError("torrent_file_not_found", "下载链接返回种子不存在", ErrorClass.ITEM)
            torrent_info_hash(content)
            return content

        try:
            return fetch_validated(choice.url)
        except ArchiveError:
            if not manual_fallback or not choice.personalized_url:
                raise
        return fetch_validated(choice.personalized_url)

    content, missing = fetch(choice.url)
    if missing:
        if not choice.personalized_url:
            raise ArchiveError(
                "torrent_file_not_found", "普通链接不存在，且没有可用的个性化链接", ErrorClass.ITEM
            )
        code = "torrent_personalized_link_required"
        if code not in decision.get("accepted_warnings", []):
            raise TorrentReviewRequired(
                {
                    **decision,
                    "warnings": [
                        *[w for w in decision.get("warnings", []) if w != code],
                        code,
                    ],
                }
            )
        content, missing = fetch(choice.personalized_url)
        if missing:
            raise ArchiveError(
                "torrent_file_not_found", "普通链接和个性化链接均返回种子不存在", ErrorClass.ITEM
            )
    if not content.startswith(b"d"):
        raise ArchiveError(
            "invalid_torrent", "torrent response is not a bencode dictionary", ErrorClass.ITEM
        )
    torrent_info_hash(content)
    return content


def torrent_info_hash(content):
    """Validate bencode and hash the original info bytes (no re-encoding)."""
    position = 0
    info_bytes = None

    def read(depth=0):
        nonlocal position, info_bytes
        if depth > 64 or position >= len(content):
            raise ValueError
        start = position
        token = content[position : position + 1]
        position += 1
        if token == b"i":
            end = content.index(b"e", position)
            value = int(content[position:end])
            position = end + 1
            return value
        if token in (b"d", b"l"):
            result = {} if token == b"d" else []
            while content[position : position + 1] != b"e":
                if token == b"d":
                    key = read(depth + 1)
                    if not isinstance(key, bytes) or key in result:
                        raise ValueError
                    value_start = position
                    result[key] = read(depth + 1)
                    if depth == 0 and key == b"info":
                        if not isinstance(result[key], dict):
                            raise ValueError
                        info_bytes = content[value_start:position]
                else:
                    result.append(read(depth + 1))
            position += 1
            return result
        if not token.isdigit():
            raise ValueError
        end = content.index(b":", start)
        size = int(content[start:end])
        if size < 0 or end + 1 + size > len(content):
            raise ValueError
        position = end + 1 + size
        return content[end + 1 : position]

    try:
        value = read()
        if (
            position != len(content)
            or not isinstance(value, dict)
            or not info_bytes
            or not value[b"info"].get(b"name")
            or not (b"length" in value[b"info"] or b"files" in value[b"info"])
        ):
            raise ValueError
    except (ValueError, IndexError, KeyError, RecursionError):
        raise ArchiveError(
            "invalid_torrent", "返回内容不是有效的种子文件", ErrorClass.ITEM
        ) from None
    return hashlib.sha1(info_bytes).hexdigest()
