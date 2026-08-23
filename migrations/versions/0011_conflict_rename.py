"""Add the Web-approved conflict rename workflow."""

import sqlalchemy as sa
from alembic import op

from eh_archive.db.models import STATUS_VALUES

revision = "0011_conflict_rename"
down_revision = "0010_web_review"
branch_labels = None
depends_on = None


def _status_constraint(values: tuple[str, ...]) -> str:
    return "status IN (" + ",".join(repr(value) for value in values) + ")"


def upgrade() -> None:
    op.add_column("manga", sa.Column("rename_target_filename", sa.Text(), nullable=True))
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    op.create_check_constraint("ck_manga_status", "manga", _status_constraint(STATUS_VALUES))


def downgrade() -> None:
    op.execute("UPDATE manga SET status = 'manual_review' WHERE status = 'rename_pending'")
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    previous_values = tuple(value for value in STATUS_VALUES if value != "rename_pending")
    op.create_check_constraint(
        "ck_manga_status",
        "manga",
        _status_constraint(previous_values),
    )
    op.drop_column("manga", "rename_target_filename")
