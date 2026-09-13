"""Decouple special workflows, preserving IDs and Manga integration state."""

import json
import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015_special_modules"
down_revision = "0014_screening_pipeline"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    columns = {c["name"] for c in inspector.get_columns("special_workflow")}
    if "manga_id" in columns:
        if connection.scalar(sa.text("SELECT count(*) FROM special_job WHERE status = 'running'")):
            raise RuntimeError("Stop special workers and resolve running leases before migration")
        if connection.scalar(
            sa.text("SELECT count(*) FROM special_workflow WHERE kind <> 'video_archive'")
        ):
            raise RuntimeError("Unknown legacy workflow kind: explicit migration required")
        op.add_column("special_workflow", sa.Column("schema_version", sa.Integer(), nullable=True))
        op.add_column(
            "special_workflow", sa.Column("cancel_requested_at", sa.DateTime(timezone=True))
        )
        op.add_column(
            "special_workflow",
            sa.Column(
                "resource_claims", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
        )
        from eh_archive.db.models import SpecialWorkflowManga

        SpecialWorkflowManga.__table__.create(connection, checkfirst=True)
        before = connection.scalar(sa.text("SELECT count(*) FROM special_workflow"))
        migrated_at = datetime.now(UTC)
        occupied = set()
        for row in connection.execute(
            sa.text("SELECT * FROM special_workflow ORDER BY id")
        ).mappings():
            payload = dict(row["payload"] or {})
            entry = payload.pop("entry", {})
            context = {
                "entry": entry,
                "expected_manga_version": None,
                "expected_artifact_generation": None,
            }
            request = payload.pop("cancel_requested", None)
            cancelled_at = None
            if request and row["status"] == "active":
                try:
                    cancelled_at = datetime.fromisoformat(request["requested_at"])
                    if cancelled_at.tzinfo is None:
                        raise ValueError("missing offset")
                except (KeyError, TypeError, ValueError):
                    cancelled_at = migrated_at
                    connection.execute(
                        sa.text("""INSERT INTO event_log
                        (manga_id,component,event_type,actor,detail,created_at)
                        VALUES (:mid,'special_processing','special_cancel_time_migrated','migration',CAST(:detail AS json),:at)"""),
                        {
                            "mid": row["manga_id"],
                            "detail": json.dumps(
                                {
                                    "workflow_id": row["id"],
                                    "time_source": "migration",
                                    "original_time_unknown": True,
                                }
                            ),
                            "at": migrated_at,
                        },
                    )
            claims = []
            if row["status"] in {"active", "failed"}:
                key = f"manga:{row['manga_id']}"
                if key in occupied:
                    raise RuntimeError(
                        "Conflicting historical resource ownership; resolve before migration"
                    )
                occupied.add(key)
                claims = [{"key": key, "scope": "workflow"}]
            connection.execute(
                sa.text("""INSERT INTO special_workflow_manga
                (workflow_id,manga_id,resume_status,context)
                VALUES (:id,:mid,:resume,CAST(:context AS jsonb))"""),
                {
                    "id": row["id"],
                    "mid": row["manga_id"],
                    "resume": row["resume_status"],
                    "context": json.dumps(context),
                },
            )
            connection.execute(
                sa.text("""UPDATE special_workflow SET schema_version=1,
                payload=CAST(:payload AS jsonb), cancel_requested_at=:cancel,
                resource_claims=CAST(:claims AS jsonb) WHERE id=:id"""),
                {
                    "id": row["id"],
                    "payload": json.dumps(payload),
                    "cancel": cancelled_at,
                    "claims": json.dumps(claims),
                },
            )
        after = connection.scalar(sa.text("SELECT count(*) FROM special_workflow_manga"))
        if before != after or connection.scalar(
            sa.text("""SELECT count(*) FROM special_workflow w
            LEFT JOIN special_workflow_manga b ON b.workflow_id=w.id AND b.manga_id=w.manga_id
            WHERE b.workflow_id IS NULL OR b.resume_status IS DISTINCT FROM w.resume_status""")
        ):
            raise RuntimeError("Workflow binding migration verification failed")
        # Normalize only associations that are verifiable; unknown events are retained.
        workflows = set(connection.scalars(sa.text("SELECT id FROM special_workflow")))
        jobs = dict(connection.execute(sa.text("SELECT id,workflow_id FROM special_job")).all())
        for event in connection.execute(
            sa.text("SELECT id,detail FROM event_log WHERE component='special_processing'")
        ).mappings():
            detail = dict(event["detail"] or {})
            old = dict(detail)
            raw = detail.get("workflow_id")
            wid = int(str(raw)) if str(raw).isdigit() else None
            jid_raw = detail.get("job_id")
            jid = int(str(jid_raw)) if str(jid_raw).isdigit() else None
            if wid is None and jid in jobs:
                wid = jobs[jid]
            if wid in workflows:
                detail["workflow_id"] = wid
                if jid in jobs and jobs[jid] == wid:
                    detail["job_id"] = jid
            if (raw is not None and wid not in workflows) or (
                jid_raw is not None and (jid not in jobs or jobs[jid] != wid)
            ):
                logging.getLogger(__name__).warning(
                    "Retained unresolved special event association: event_id=%s", event["id"]
                )
            if detail != old:
                connection.execute(
                    sa.text("UPDATE event_log SET detail=CAST(:detail AS json) WHERE id=:id"),
                    {"id": event["id"], "detail": json.dumps(detail)},
                )
        op.alter_column("special_workflow", "schema_version", nullable=False)
        op.create_check_constraint(
            "ck_special_workflow_schema_version", "special_workflow", "schema_version > 0"
        )
        op.create_check_constraint(
            "ck_special_workflow_resource_claims_array",
            "special_workflow",
            "jsonb_typeof(resource_claims) = 'array'",
        )
        op.create_index(
            "ix_special_workflow_resource_claims",
            "special_workflow",
            ["resource_claims"],
            postgresql_using="gin",
            postgresql_ops={"resource_claims": "jsonb_path_ops"},
        )
        op.drop_index("uq_special_workflow_active_manga", table_name="special_workflow")
        for fk in inspector.get_foreign_keys("special_workflow"):
            if "manga_id" in fk["constrained_columns"]:
                op.drop_constraint(fk["name"], "special_workflow", type_="foreignkey")
        op.drop_column("special_workflow", "manga_id")
        op.drop_column("special_workflow", "resume_status")
    # 0001 creates current metadata on clean installations; don't add it twice.
    for key in ("workflow", "job"):
        connection.execute(
            sa.text(f"""CREATE INDEX IF NOT EXISTS ix_event_special_{key}_created
            ON event_log ((detail ->> '{key}_id'), created_at DESC, id DESC)
            WHERE component = 'special_processing'""")
        )


def downgrade():
    raise RuntimeError(
        "Restore the pre-migration backup and matching application; new independent workflows cannot be downgraded safely"
    )
