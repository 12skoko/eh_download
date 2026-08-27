"""Add persistent special-processing workflows and one-shot jobs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_special_processing"
down_revision = "0012_attempt_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    op.create_check_constraint(
        "ck_manga_status",
        "manga",
        "status IN ("
        "'discovered','deferred','download_pending','downloading','downloaded',"
        "'validating','preparing','upload_pending','uploading','uploaded','completed',"
        "'quarantined','manual_review','special_processing','skipped','unavailable',"
        "'outdated','force_delete_pending','rename_pending','deleted','cancel_requested',"
        "'cancelled')",
    )
    op.alter_column(
        "event_log",
        "operation",
        existing_type=sa.String(length=24),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    # 0001_initial intentionally builds from current SQLAlchemy metadata.  On
    # a brand-new installation that means these two current tables can already
    # exist before Alembic reaches this historical revision; an incremental
    # production upgrade from 0012 will not have them yet.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("special_workflow") and inspector.has_table("special_job"):
        return
    op.create_table(
        "special_workflow",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("manga_id", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("resume_status", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'failed', 'cancelled')",
            name="ck_special_workflow_status",
        ),
        sa.CheckConstraint("row_version >= 0", name="ck_special_workflow_row_version"),
        sa.ForeignKeyConstraint(["manga_id"], ["manga.manga_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_special_workflow_active_manga",
        "special_workflow",
        ["manga_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_special_workflow_status_updated",
        "special_workflow",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_special_workflow_kind_phase",
        "special_workflow",
        ["kind", "status", "phase"],
    )
    op.create_table(
        "special_job",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workflow_id", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger_source", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("external_effect_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_no >= 1", name="ck_special_job_attempt_no"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'abandoned', 'cancelled')",
            name="ck_special_job_status",
        ),
        sa.CheckConstraint(
            "trigger_source IN ('web', 'cli', 'system')",
            name="ck_special_job_trigger_source",
        ),
        sa.ForeignKeyConstraint(["workflow_id"], ["special_workflow.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id", "operation", "attempt_no", name="uq_special_job_operation_no"
        ),
    )
    op.create_index("ix_special_job_queue", "special_job", ["status", "next_run_at", "created_at"])
    op.create_index("ix_special_job_workflow_created", "special_job", ["workflow_id", "created_at"])
    op.create_index(
        "uq_special_job_running_workflow",
        "special_job",
        ["workflow_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "uq_special_job_active_operation",
        "special_job",
        ["workflow_id", "operation"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_special_job_active_operation", table_name="special_job")
    op.drop_index("uq_special_job_running_workflow", table_name="special_job")
    op.drop_index("ix_special_job_workflow_created", table_name="special_job")
    op.drop_index("ix_special_job_queue", table_name="special_job")
    op.drop_table("special_job")
    op.drop_index("ix_special_workflow_kind_phase", table_name="special_workflow")
    op.drop_index("ix_special_workflow_status_updated", table_name="special_workflow")
    op.drop_index("uq_special_workflow_active_manga", table_name="special_workflow")
    op.drop_table("special_workflow")
    op.execute("UPDATE manga SET status = 'manual_review' WHERE status = 'special_processing'")
    op.execute("DELETE FROM event_log WHERE component = 'special_processing'")
    op.alter_column(
        "event_log",
        "operation",
        existing_type=sa.String(length=64),
        type_=sa.String(length=24),
        existing_nullable=True,
    )
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    op.create_check_constraint(
        "ck_manga_status",
        "manga",
        "status IN ("
        "'discovered','deferred','download_pending','downloading','downloaded',"
        "'validating','preparing','upload_pending','uploading','uploaded','completed',"
        "'quarantined','manual_review','skipped','unavailable','outdated',"
        "'force_delete_pending','rename_pending','deleted','cancel_requested','cancelled')",
    )
