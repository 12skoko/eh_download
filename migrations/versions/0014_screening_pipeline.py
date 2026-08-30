"""Separate collection timing from on-demand screening."""

import sqlalchemy as sa
from alembic import op

revision = "0014_screening_pipeline"
down_revision = "0013_special_processing"
branch_labels = None
depends_on = None


_STATUS_CONSTRAINT = (
    "status IN ("
    "'discovered','deferred','download_pending','downloading','downloaded',"
    "'validating','preparing','upload_pending','uploading','uploaded','completed',"
    "'quarantined','manual_review','special_processing','filtered_out','skipped',"
    "'unavailable','outdated','force_delete_pending','rename_pending','deleted',"
    "'cancel_requested','cancelled')"
)


def upgrade() -> None:
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    op.create_check_constraint("ck_manga_status", "manga", _STATUS_CONSTRAINT)
    op.execute(
        "UPDATE manga SET status = 'filtered_out' "
        "WHERE status = 'discovered' AND screen_pending = FALSE "
        "AND remark IN ('excluded_category', 'screen_not_eligible')"
    )
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("manga")}
    if "ix_manga_screen_pending" in indexes:
        op.drop_index("ix_manga_screen_pending", table_name="manga")
    columns = {item["name"] for item in inspector.get_columns("manga")}
    if "screen_pending" in columns:
        op.drop_column("manga", "screen_pending")


def downgrade() -> None:
    op.add_column(
        "manga",
        sa.Column("screen_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_manga_screen_pending", "manga", ["screen_pending", "status"])
    op.execute("UPDATE manga SET screen_pending = TRUE WHERE status = 'discovered'")
    op.execute("UPDATE manga SET status = 'discovered' WHERE status = 'filtered_out'")
    op.alter_column("manga", "screen_pending", server_default=None)
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    op.create_check_constraint(
        "ck_manga_status",
        "manga",
        "status IN ("
        "'discovered','deferred','download_pending','downloading','downloaded',"
        "'validating','preparing','upload_pending','uploading','uploaded','completed',"
        "'quarantined','manual_review','special_processing','skipped','unavailable',"
        "'outdated','force_delete_pending','rename_pending','deleted','cancel_requested',"
        "'cancelled')",
    )
