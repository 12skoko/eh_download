from sqlalchemy import select

from ...db.models import MangaRecord, SpecialWorkflow, SpecialWorkflowManga
from ..core.contracts import Integration


def binding(workflow):
    if len(workflow.manga_bindings) != 1:
        raise ValueError("此模块要求恰好关联一个档案")
    return workflow.manga_bindings[0]


def active_for_manga(session, manga_id):
    return session.scalar(
        select(SpecialWorkflow)
        .join(SpecialWorkflowManga)
        .where(SpecialWorkflowManga.manga_id == manga_id, SpecialWorkflow.status == "active")
    )


def bind(session, workflow, manga, *, entry, resume_status=None):
    row = SpecialWorkflowManga(
        manga_id=manga.manga_id,
        resume_status=resume_status,
        context={
            "entry": dict(entry),
            "expected_manga_version": manga.row_version,
            "expected_artifact_generation": manga.artifact_generation,
        },
    )
    workflow.manga_bindings.append(row)
    session.flush()
    return row


class MangaIntegration(Integration):
    def event_subject(self, workflow):
        return workflow.manga_bindings[0].manga_id if len(workflow.manga_bindings) == 1 else None

    def resources(self, session, workflow, job):
        return tuple(f"manga:{b.manga_id}" for b in workflow.manga_bindings)

    def records(self, session, workflow):
        return tuple(
            session.scalars(
                select(MangaRecord)
                .where(MangaRecord.manga_id.in_([b.manga_id for b in workflow.manga_bindings]))
                .order_by(MangaRecord.manga_id)
                .with_for_update()
            )
        )
