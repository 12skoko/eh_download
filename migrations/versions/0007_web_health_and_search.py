"""Add Web health snapshots and indexes for the management interface."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_web_health"
down_revision = "0006_supervisor_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("system_health"):
        op.create_table(
            "system_health",
            sa.Column("component", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="unknown"),
            sa.Column(
                "checked_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("error_code", sa.Text(), nullable=True),
            sa.Column("message", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "detail",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "status IN ('healthy', 'degraded', 'unavailable', 'unknown')",
                name="ck_system_health_status",
            ),
            sa.PrimaryKeyConstraint("component"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_system_health_checked ON system_health (checked_at)")

    # Existing installations can contain hundreds of thousands of rows. Build
    # trigram indexes without holding a long write lock on the manga table.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_web_queue "
            "ON manga (priority DESC, created_at, manga_id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_web_error "
            "ON manga (last_error_at DESC, manga_id) WHERE last_error_at IS NOT NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_web_retry "
            "ON manga (next_retry_at, manga_id) WHERE next_retry_at IS NOT NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_event_web_created "
            "ON event_log (created_at DESC, id DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_name_trgm "
            "ON manga USING gin (lower(name) gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_real_name_trgm "
            "ON manga USING gin (lower(real_name) gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_real_name_trgm")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_name_trgm")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_event_web_created")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_web_retry")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_web_error")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_web_queue")
    if sa.inspect(op.get_bind()).has_table("system_health"):
        op.drop_index("ix_system_health_checked", table_name="system_health")
        op.drop_table("system_health")
    # pg_trgm may be shared with other applications, so downgrade leaves it installed.
