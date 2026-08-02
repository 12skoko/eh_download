"""Remove the legacy torrent/H@H prepared artifact locations."""

import sqlalchemy as sa
from alembic import op

# Alembic's default alembic_version.version_num is VARCHAR(32).
revision = "0002_prepared_locations"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_CURRENT_LOCATION_CHECK = (
    "artifact_location IS NULL OR artifact_location IN "
    "('torrent_download', 'hah_download', 'direct_download', "
    "'aria2_download', 'prepared', 'quarantine', 'trash')"
)
_LEGACY_LOCATION_CHECK = (
    "artifact_location IS NULL OR artifact_location IN "
    "('torrent_download', 'torrent_prepared', 'hah_download', 'hah_prepared', "
    "'direct_download', 'aria2_download', 'prepared', 'quarantine', 'trash')"
)


def upgrade() -> None:
    op.drop_constraint("ck_manga_artifact_location", "manga", type_="check")
    op.execute(
        sa.text(
            "UPDATE manga SET artifact_location = 'prepared' "
            "WHERE artifact_location IN ('torrent_prepared', 'hah_prepared')"
        )
    )
    op.create_check_constraint("ck_manga_artifact_location", "manga", _CURRENT_LOCATION_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_manga_artifact_location", "manga", type_="check")
    op.create_check_constraint("ck_manga_artifact_location", "manga", _LEGACY_LOCATION_CHECK)
