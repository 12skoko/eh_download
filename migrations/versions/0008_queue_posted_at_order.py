"""Index the archive queue in descending gallery-posted order."""

from alembic import op

revision = "0008_queue_posted"
down_revision = "0007_web_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the replacement before removing the old index, so the queue keeps
    # an available ordering index throughout a production upgrade.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_web_posted "
            "ON manga (posted_at DESC NULLS LAST, manga_id)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_web_queue")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_web_queue "
            "ON manga (priority DESC, created_at, manga_id)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_web_posted")
