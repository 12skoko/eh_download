from __future__ import annotations

from ...config import CrawlConfig
from ...db.models import MangaRecord
from ...db.repository import ArchiveRepository
from ...domain.models import Manga
from ...domain.states import Status
from .models import ScreenDecision, ScreeningBatchResult
from .policy import ScreenAction, ScreeningPolicy, screen_group_id, screen_priority, select_versions


def _manga(row: MangaRecord) -> Manga:
    return Manga(
        manga_id=row.manga_id,
        name=row.name,
        link=row.link,
        real_name=row.real_name,
        posted_at=row.posted_at,
        category=row.category,
        tags_raw=row.tags_raw,
        pages=row.pages,
        rating=row.rating,
        uploader=row.uploader,
    )


class ScreeningService:
    def __init__(self, repository: ArchiveRepository, crawl: CrawlConfig) -> None:
        self.repository = repository
        self.policy = ScreeningPolicy(
            name_keywords=crawl.name_keywords,
            tag_keywords=crawl.tag_keywords,
            exclude_categories=crawl.exclude_categories,
        )

    def run_batch(self, limit: int = 100, *, actor: str = "screen") -> ScreeningBatchResult:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        result = ScreeningBatchResult()
        pending_rows = self.repository.list_screen_candidates(limit=limit)
        visited_groups: set[str] = set()
        for row in pending_rows:
            if row.status != Status.DISCOVERED.value:
                continue
            eligibility = self.policy.evaluate(_manga(row))
            if eligibility.action == ScreenAction.FILTER_OUT:
                self._apply_single(
                    row,
                    Status.FILTERED_OUT,
                    eligibility.reason,
                    result,
                    actor=actor,
                )
            elif eligibility.action == ScreenAction.QUEUE:
                self._apply_single(
                    row,
                    Status.DOWNLOAD_PENDING,
                    eligibility.reason,
                    result,
                    actor=actor,
                )
            elif not row.real_name:
                self._apply_single(
                    row,
                    Status.MANUAL_REVIEW,
                    "missing_real_name",
                    result,
                    actor=actor,
                )
            elif row.real_name not in visited_groups:
                visited_groups.add(row.real_name)
                self._apply_group(row.real_name, result, actor=actor)
        return result

    def _apply_single(
        self,
        row: MangaRecord,
        status: Status,
        reason: str,
        result: ScreeningBatchResult,
        *,
        actor: str,
    ) -> None:
        self.repository.apply_screen_outcome(
            row,
            status=status,
            reason=reason,
            actor=actor,
        )
        result.record(
            ScreenDecision(
                manga_id=row.manga_id,
                real_name=row.real_name,
                resulting_status=status.value,
                reason=reason,
            )
        )

    def _apply_group(
        self,
        real_name: str,
        result: ScreeningBatchResult,
        *,
        actor: str,
    ) -> None:
        similar = self.repository.list_screen_group(real_name)
        relation = screen_group_id(real_name)
        evaluations = {
            row.manga_id: self.policy.evaluate(_manga(row))
            for row in similar
            if row.status == Status.DISCOVERED.value
        }
        competing: list[MangaRecord] = []
        for row in similar:
            if row.status in {Status.DEFERRED.value, Status.FILTERED_OUT.value}:
                continue
            if (
                row.status == Status.DISCOVERED.value
                and evaluations[row.manga_id].action == ScreenAction.FILTER_OUT
            ):
                continue
            competing.append(row)
        priorities = [screen_priority(_manga(row)) for row in competing]
        selected_indexes = (
            {0}
            if len(competing) == 1
            else {index for index, selected in enumerate(select_versions(priorities)) if selected}
        )
        selected_ids = {competing[index].manga_id for index in selected_indexes}
        candidate_count = len(competing)

        for row in similar:
            if row.status != Status.DISCOVERED.value:
                continue
            eligibility = evaluations[row.manga_id]
            if eligibility.action == ScreenAction.FILTER_OUT:
                status = Status.FILTERED_OUT
                reason = eligibility.reason
                selected: bool | None = None
                priority: float | None = None
                group_id: str | None = None
            elif eligibility.action == ScreenAction.QUEUE:
                status = Status.DOWNLOAD_PENDING
                reason = eligibility.reason
                selected = True
                priority = screen_priority(_manga(row))
                group_id = relation
            else:
                selected = row.manga_id in selected_ids
                status = Status.DOWNLOAD_PENDING if selected else Status.SKIPPED
                reason = "version_selected" if selected else "version_rejected"
                priority = screen_priority(_manga(row))
                group_id = relation
            self.repository.apply_screen_outcome(
                row,
                status=status,
                reason=reason,
                actor=actor,
                screen_group_id=group_id,
                detail={
                    "screen_group_id": group_id,
                    "selected": selected,
                    "candidate_count": candidate_count,
                    "priority": priority,
                },
            )
            result.record(
                ScreenDecision(
                    manga_id=row.manga_id,
                    real_name=real_name,
                    resulting_status=status.value,
                    reason=reason,
                    screen_group_id=group_id,
                    candidate_count=candidate_count,
                    selected=selected,
                    priority=priority,
                )
            )
