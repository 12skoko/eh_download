from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScreenDecision:
    manga_id: str
    real_name: str
    resulting_status: str
    reason: str
    screen_group_id: str | None = None
    candidate_count: int = 0
    selected: bool | None = None
    priority: float | None = None


@dataclass
class ScreeningBatchResult:
    processed: int = 0
    queued: int = 0
    filtered_out: int = 0
    skipped: int = 0
    manual_review: int = 0
    decisions: list[ScreenDecision] = field(default_factory=list)

    def record(self, decision: ScreenDecision) -> None:
        self.processed += 1
        self.decisions.append(decision)
        counter = {
            "download_pending": "queued",
            "filtered_out": "filtered_out",
            "skipped": "skipped",
            "manual_review": "manual_review",
        }.get(decision.resulting_status)
        if counter is not None:
            setattr(self, counter, getattr(self, counter) + 1)
