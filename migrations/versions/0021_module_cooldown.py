"""Expose process-owned EH cooldowns and requests to release them."""

import sqlalchemy as sa
from alembic import op

revision = "0021_module_cooldown"
down_revision = "0020_module_schedule"
branch_labels = None
depends_on = None


def upgrade():
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("system_control")}
    for column in (
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("cooldown_reason", sa.Text()),
        sa.Column("cooldown_owner", sa.Text()),
        sa.Column("cooldown_release_requested", sa.Boolean()),
    ):
        if column.name not in existing:
            op.add_column("system_control", column)


def downgrade():
    for name in ("cooldown_release_requested", "cooldown_owner", "cooldown_reason", "cooldown_until"):
        op.drop_column("system_control", name)
