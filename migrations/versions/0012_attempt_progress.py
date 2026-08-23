"""Add observable progress snapshots for running attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0012_attempt_progress"
down_revision = "0011_conflict_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_attempt", sa.Column("progress_bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "job_attempt", sa.Column("progress_total_bytes", sa.BigInteger(), nullable=True)
    )
    op.add_column("job_attempt", sa.Column("progress_speed_bps", sa.Float(), nullable=True))
    op.add_column(
        "job_attempt",
        sa.Column("progress_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_attempt", "progress_updated_at")
    op.drop_column("job_attempt", "progress_speed_bps")
    op.drop_column("job_attempt", "progress_total_bytes")
    op.drop_column("job_attempt", "progress_bytes")
