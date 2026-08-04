"""Associate collect events with a lightweight run identifier."""

import sqlalchemy as sa
from alembic import op

revision = "0004_collect_run_id"
down_revision = "0003_screen_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("event_log")}
    if "run_id" not in columns:
        op.add_column("event_log", sa.Column("run_id", sa.String(length=36), nullable=True))

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("event_log")}
    if "ix_event_run_created" not in indexes:
        op.create_index("ix_event_run_created", "event_log", ["run_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("event_log")}
    if "ix_event_run_created" in indexes:
        op.drop_index("ix_event_run_created", table_name="event_log")
    columns = {column["name"] for column in inspector.get_columns("event_log")}
    if "run_id" in columns:
        op.drop_column("event_log", "run_id")
