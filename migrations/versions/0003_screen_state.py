"""Persist the distinction between discovered and screenall candidates."""

import sqlalchemy as sa
from alembic import op

revision = "0003_screen_state"
down_revision = "0002_prepared_locations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("manga")}
    if "screen_pending" not in columns:
        op.add_column(
            "manga",
            sa.Column("screen_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "screen_group_id" not in columns:
        op.add_column("manga", sa.Column("screen_group_id", sa.String(length=64), nullable=True))
    indexes = {index["name"] for index in inspector.get_indexes("manga")}
    if "ix_manga_screen_pending" not in indexes:
        op.create_index("ix_manga_screen_pending", "manga", ["screen_pending", "status"])

    # The migration importer records legacy autostate in the audit event. Use
    # that information to restore autostate=1 rows already imported before
    # this revision existed; state=1/autostate=NULL remains plain discovered.
    op.execute(
        sa.text(
            "UPDATE manga AS m SET screen_pending = TRUE "
            "WHERE m.status = 'discovered' AND EXISTS ("
            "SELECT 1 FROM event_log AS e "
            "WHERE e.manga_id = m.manga_id "
            "AND e.event_type = 'legacy_import' "
            "AND e.detail ->> 'legacy_autostate' = '1'"
            ")"
        )
    )
    # Legacy autostate=-1 represented a one-day observation period but had no
    # equivalent timestamp column. Populate the due time so Supervisor can
    # immediately rejudge old deferred rows after the upgrade.
    op.execute(
        sa.text(
            "UPDATE manga SET defer_until = posted_at + INTERVAL '1 day' "
            "WHERE status = 'deferred' AND defer_until IS NULL AND posted_at IS NOT NULL"
        )
    )
    op.alter_column("manga", "screen_pending", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("manga")}
    columns = {column["name"] for column in inspector.get_columns("manga")}
    if "ix_manga_screen_pending" in indexes:
        op.drop_index("ix_manga_screen_pending", table_name="manga")
    if "screen_group_id" in columns:
        op.drop_column("manga", "screen_group_id")
    if "screen_pending" in columns:
        op.drop_column("manga", "screen_pending")
