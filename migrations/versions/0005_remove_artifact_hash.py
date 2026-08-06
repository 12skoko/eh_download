"""Remove the unused local SHA-256 artifact fingerprint."""

import sqlalchemy as sa
from alembic import op

revision = "0005_remove_artifact_hash"
down_revision = "0004_collect_run_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("manga")}
    if "artifact_hash" in columns:
        op.drop_column("manga", "artifact_hash")


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("manga")}
    if "artifact_hash" not in columns:
        op.add_column("manga", sa.Column("artifact_hash", sa.String(length=64), nullable=True))
