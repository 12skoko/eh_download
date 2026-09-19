"""Expose interval schedules and owner-bound manual requests."""

import sqlalchemy as sa
from alembic import op

revision = "0020_module_schedule"
down_revision = "0019_torrent_review"
branch_labels = None
depends_on = None


def upgrade():
    # 0001 creates the current ORM metadata on a fresh installation.
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("system_control")}
    columns = []
    for name in ("next_run_at", "schedule_updated_at", "trigger_requested_at"):
        columns.append(sa.Column(name, sa.DateTime(timezone=True)))
    columns.append(sa.Column("schedule_running", sa.Boolean()))
    for name in ("schedule_block_reason", "trigger_owner", "trigger_message"):
        columns.append(sa.Column(name, sa.Text()))
    columns.append(sa.Column("trigger_status", sa.String(16)))
    for column in columns:
        if column.name not in existing:
            op.add_column("system_control", column)


def downgrade():
    for name in (
        "trigger_status", "trigger_message", "trigger_owner", "schedule_block_reason",
        "schedule_running", "trigger_requested_at", "schedule_updated_at", "next_run_at",
    ):
        op.drop_column("system_control", name)
