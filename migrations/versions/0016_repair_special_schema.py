"""Repair databases stamped at 0015 while retaining the legacy special schema."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0016_repair_special_schema"
down_revision = "0015_special_modules"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    connection.execute(sa.text(
        "LOCK TABLE special_workflow, special_job IN ACCESS EXCLUSIVE MODE"
    ))
    columns = {c["name"] for c in sa.inspect(connection).get_columns("special_workflow")}
    if "manga_id" in columns:
        spec = spec_from_file_location(
            "repair_special_0015", Path(__file__).with_name("0015_special_modules.py")
        )
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.upgrade()
    inspector = sa.inspect(connection)
    columns = {c["name"] for c in inspector.get_columns("special_workflow")}
    if not {"schema_version", "cancel_requested_at", "resource_claims"} <= columns:
        raise RuntimeError("Incomplete special workflow schema; restore or repair before startup")
    if not inspector.has_table("special_workflow_manga"):
        raise RuntimeError("Missing workflow bindings; restore from backup, do not fabricate bindings")


def downgrade():
    raise RuntimeError("Restore the pre-migration backup and matching application")
