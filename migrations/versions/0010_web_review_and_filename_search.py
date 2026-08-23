"""Index filename search and the manual-review work queue."""

from alembic import op

revision = "0010_web_review"
down_revision = "0009_force_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_artifact_filename_trgm "
            "ON manga USING gin (lower(artifact_filename) gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_web_review "
            "ON manga (status, status_updated_at DESC, manga_id) "
            "WHERE status IN ('manual_review', 'quarantined')"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_web_review")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_artifact_filename_trgm")
