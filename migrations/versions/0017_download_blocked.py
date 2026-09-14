"""Add a parked state for deployments without an archive fallback method."""

import sqlalchemy as sa
from alembic import op

revision = "0017_download_blocked"
down_revision = "0016_repair_special_schema"
branch_labels = None
depends_on = None


_STATUS_CONSTRAINT = (
    "status IN ("
    "'discovered','deferred','download_pending','download_blocked','downloading','downloaded',"
    "'validating','preparing','upload_pending','uploading','uploaded','completed',"
    "'quarantined','manual_review','special_processing','filtered_out','skipped',"
    "'unavailable','outdated','force_delete_pending','rename_pending','deleted',"
    "'cancel_requested','cancelled')"
)

_PREVIOUS_STATUS_CONSTRAINT = (
    "status IN ("
    "'discovered','deferred','download_pending','downloading','downloaded',"
    "'validating','preparing','upload_pending','uploading','uploaded','completed',"
    "'quarantined','manual_review','special_processing','filtered_out','skipped',"
    "'unavailable','outdated','force_delete_pending','rename_pending','deleted',"
    "'cancel_requested','cancelled')"
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    connection.execute(sa.text("LOCK TABLE manga IN ACCESS EXCLUSIVE MODE"))
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    op.create_check_constraint("ck_manga_status", "manga", _STATUS_CONSTRAINT)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    connection.execute(sa.text("LOCK TABLE manga IN ACCESS EXCLUSIVE MODE"))
    connection.execute(
        sa.text(
            "UPDATE manga SET status = 'download_pending' "
            "WHERE status = 'download_blocked'"
        )
    )
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    op.create_check_constraint("ck_manga_status", "manga", _PREVIOUS_STATUS_CONSTRAINT)
