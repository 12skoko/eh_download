from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ...domain.models import Manga
from ...domain.screen_rules import V2_ALGORITHM, RuleTrace, compile_rule, normalized_eh_tags


class ScreenAction(StrEnum):
    FILTER_OUT = "filter_out"
    QUEUE = "queue"
    COMPARE_GROUP = "compare_group"


@dataclass(frozen=True)
class EligibilityDecision:
    action: ScreenAction
    reason: str


def _contains_key(text: str, keyword: str) -> bool:
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text))


class ScreeningPolicy:
    def __init__(
        self, direct_json: str = "null", algorithm_json: str = V2_ALGORITHM,
        *, trace: RuleTrace | None = None,
    ):
        today = datetime.now(UTC).date()
        self.direct = compile_rule(direct_json, "screening.direct_json", today=today, trace=trace)
        self.algorithm = compile_rule(
            algorithm_json, "screening.algorithm_json", today=today, trace=trace
        )

    def evaluate(self, manga: Manga) -> EligibilityDecision:
        if self.direct(manga):
            return EligibilityDecision(ScreenAction.QUEUE, "direct_rule")
        if self.algorithm(manga):
            return EligibilityDecision(ScreenAction.COMPARE_GROUP, "algorithm_rule")
        return EligibilityDecision(ScreenAction.FILTER_OUT, "screen_not_eligible")


def select_versions(similar_flag_list: list[float]) -> list[int]:
    """Apply the legacy tier-and-variant version selection algorithm."""

    result = [0] * len(similar_flag_list)
    tiers: dict[int, list[tuple[float, int]]] = {1: [], 2: [], 3: []}
    for index, value in enumerate(similar_flag_list):
        tier = int(value // 10)
        if tier in tiers:
            fraction = round(value - tier * 10, 12)
            tiers[tier].append((fraction, index))
    selected = tiers[3] or tiers[2] or tiers[1]
    by_variant: dict[int, list[tuple[float, int]]] = {}
    for fraction, index in selected:
        variant = int(fraction)
        by_variant.setdefault(variant, []).append((round(fraction - variant, 12), index))
    for candidates in by_variant.values():
        _score, index = max(candidates, key=lambda item: item[0])
        result[index] = 1
    return result


def screen_group_id(real_name: str) -> str:
    digest = hashlib.sha1(real_name.encode("utf-8")).hexdigest()
    return f"screen-{digest}"


def screen_priority(manga: Manga) -> float:
    chinese = "chinese" in manga.tags_raw.lower()
    uncensored = (
        "無修正" in manga.name
        or "无修正" in manga.name
        or _contains_key(manga.name.casefold(), "uncensored")
        or "other:uncensored" in normalized_eh_tags(manga.tags_raw)
    )
    rating = manga.rating or 0
    if uncensored:
        if chinese:
            base = 31 if rating > 30 else 22
        else:
            base = 21
    elif chinese:
        base = 23 if rating > 30 else 12
    else:
        base = 11
    posted_at = manga.posted_at
    if posted_at is not None and posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    timestamp = int(posted_at.timestamp()) if posted_at else 0
    return base + rating * 0.01 + timestamp * 0.000000000001
