"""Index replacement dependencies used by upload scheduling."""

from alembic import op

revision = "0018_superseded_upload_wait"
down_revision = "0017_download_blocked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_manga_superseded_status "
            "ON manga (superseded_by_id, status)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_manga_superseded_status")
