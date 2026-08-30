from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum

from ...domain.models import Manga


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


@dataclass(frozen=True)
class ScreeningPolicy:
    name_keywords: tuple[str, ...] = ()
    tag_keywords: tuple[str, ...] = ()
    exclude_categories: tuple[str, ...] = ()

    def evaluate(self, manga: Manga) -> EligibilityDecision:
        if manga.category in self.exclude_categories:
            return EligibilityDecision(ScreenAction.FILTER_OUT, "excluded_category")

        lowered_tags = manga.tags_raw.lower()
        languages = {
            "english",
            "korean",
            "russian",
            "french",
            "dutch",
            "hungarian",
            "italian",
            "polish",
            "portuguese",
            "spanish",
            "thai",
            "vietnamese",
            "ukrainian",
        }
        if (
            "translated" in lowered_tags
            and "chinese" not in lowered_tags
            and any(language in lowered_tags for language in languages)
        ):
            return EligibilityDecision(ScreenAction.FILTER_OUT, "unsupported_translation")
        if any(
            _contains_key(manga.name.lower(), keyword.lower()) for keyword in self.name_keywords
        ):
            return EligibilityDecision(ScreenAction.QUEUE, "name_keyword")
        if any(
            _contains_key(manga.tags_raw.lower(), keyword.lower()) for keyword in self.tag_keywords
        ):
            return EligibilityDecision(ScreenAction.QUEUE, "tag_keyword")
        if manga.category in {"Manga", "Doujinshi"} and (
            "chinese" in lowered_tags or (manga.rating or 0) >= 30
        ):
            return EligibilityDecision(ScreenAction.COMPARE_GROUP, "eligible_for_version_selection")
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
    uncensored = "無修正" in manga.name or "无修正" in manga.name
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
