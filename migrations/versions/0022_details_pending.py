"""Gate new downloads on details and allow uncollected replacement IDs."""

import sqlalchemy as sa
from alembic import op

revision = "0022_details_pending"
down_revision = "0021_module_cooldown"
branch_labels = None
depends_on = None

OLD_STATUSES = (
    "discovered", "deferred", "download_pending", "download_blocked", "downloading",
    "downloaded", "validating", "preparing", "upload_pending", "uploading", "uploaded",
    "completed", "quarantined", "manual_review", "special_processing", "filtered_out",
    "skipped", "unavailable", "outdated", "force_delete_pending", "rename_pending",
    "deleted", "cancel_requested", "cancelled",
)


def _lock():
    connection = op.get_bind()
    connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    connection.execute(sa.text("LOCK TABLE manga IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM manga WHERE active_attempt_id IS NOT NULL "
        "OR lease_until IS NOT NULL OR lease_owner IS NOT NULL OR lease_token IS NOT NULL)"
    )):
        raise RuntimeError("Stop ordinary workers and resolve their leases before migrating")
    return connection


def _status_constraint(values):
    op.drop_constraint("ck_manga_status", "manga", type_="check")
    op.create_check_constraint("ck_manga_status", "manga",
                               "status IN (" + ",".join(repr(v) for v in values) + ")")


def upgrade():
    connection = _lock()
    inspector = sa.inspect(connection)
    for fk in inspector.get_foreign_keys("manga"):
        if fk["constrained_columns"] == ["superseded_by_id"]:
            op.drop_constraint(fk["name"], "manga", type_="foreignkey")
    # Initial migration creates current metadata on fresh installations.
    if "archive_url" not in {c["name"] for c in inspector.get_columns("mangainfo")}:
        op.add_column("mangainfo", sa.Column("archive_url", sa.Text(), nullable=True))
    _status_constraint((*OLD_STATUSES, "details_pending"))
    connection.execute(sa.text("""
        WITH moved AS (
            UPDATE manga SET status = 'details_pending', row_version = row_version + 1,
                status_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                next_retry_at = NULL
            WHERE status = 'download_pending' AND external_download_id IS NULL
              AND manga_id NOT LIKE 'picacg/%'
            RETURNING manga_id
        )
        INSERT INTO event_log (manga_id, component, event_type, actor, from_status, to_status, detail, created_at)
        SELECT manga_id, 'migration', 'status_changed', '0022_details_pending',
               'download_pending', 'details_pending', '{"reason":"details_gate_migration"}'::jsonb,
               CURRENT_TIMESTAMP
        FROM moved
    """))


def downgrade():
    connection = _lock()
    if connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM manga a WHERE a.superseded_by_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM manga b WHERE b.manga_id = a.superseded_by_id))"
    )):
        raise RuntimeError("Resolve uncollected replacement IDs before restoring the foreign key")
    connection.execute(sa.text(
        "UPDATE manga SET status = 'download_pending', row_version = row_version + 1, "
        "updated_at = CURRENT_TIMESTAMP, status_updated_at = CURRENT_TIMESTAMP "
        "WHERE status = 'details_pending'"
    ))
    _status_constraint(OLD_STATUSES)
    op.create_foreign_key("manga_superseded_by_id_fkey", "manga", "manga",
                          ["superseded_by_id"], ["manga_id"], ondelete="RESTRICT")
    op.drop_column("mangainfo", "archive_url")
