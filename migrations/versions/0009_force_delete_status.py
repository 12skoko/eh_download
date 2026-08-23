"""Add the Web-only forced deletion queue status."""

from alembic import op

from eh_archive.db.models import STATUS_VALUES

revision = "0009_force_delete"
down_revision = "0008_queue_posted"
branch_labels = None
depends_on = None


def _status_constraint(values: tuple[str, ...]) -> str:
    return "status IN (" + ",".join(repr(value) for value in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    op.create_check_constraint("ck_manga_status", "manga", _status_constraint(STATUS_VALUES))


def downgrade() -> None:
    op.execute("UPDATE manga SET status = 'manual_review' WHERE status = 'force_delete_pending'")
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    previous_values = tuple(value for value in STATUS_VALUES if value != "force_delete_pending")
    op.create_check_constraint(
        "ck_manga_status",
        "manga",
        _status_constraint(previous_values),
    )
