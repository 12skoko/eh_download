"""Persist torrent warning decisions separately from remarks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_torrent_review"
down_revision = "0018_superseded_upload_wait"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "manga",
        sa.Column(
            "torrent_review",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade():
    op.drop_column("manga", "torrent_review")
