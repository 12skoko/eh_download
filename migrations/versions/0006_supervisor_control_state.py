"""Make the Supervisor control state authoritative for global scheduling."""

import sqlalchemy as sa
from alembic import op

revision = "0006_supervisor_control"
down_revision = "0005_remove_artifact_hash"
branch_labels = None
depends_on = None


def _replace_state_constraint(allowed: str) -> None:
    bind = op.get_bind()
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("system_control")
        if constraint.get("name")
    }
    with op.batch_alter_table("system_control") as batch:
        if "ck_system_control_state" in constraints:
            batch.drop_constraint("ck_system_control_state", type_="check")
        batch.create_check_constraint("ck_system_control_state", f"state IN ({allowed})")


def upgrade() -> None:
    _replace_state_constraint("'running', 'paused', 'draining'")

    bind = op.get_bind()
    control = sa.table(
        "system_control",
        sa.column("component", sa.String()),
        sa.column("state", sa.String()),
        sa.column("reason", sa.Text()),
        sa.column("updated_by", sa.Text()),
        sa.column("row_version", sa.BigInteger()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    all_control = bind.execute(
        sa.select(control.c.state, control.c.reason).where(control.c.component == "all")
    ).first()
    if all_control is not None and all_control.state == "paused":
        supervisor_exists = bind.execute(
            sa.select(control.c.component).where(control.c.component == "supervisor")
        ).first()
        if supervisor_exists is None:
            bind.execute(
                control.insert().values(
                    component="supervisor",
                    state="paused",
                    reason=all_control.reason or "migrated from all=paused",
                    updated_by="migration-0006",
                    row_version=0,
                    updated_at=sa.func.now(),
                )
            )
        else:
            bind.execute(
                control.update()
                .where(control.c.component == "supervisor")
                .values(
                    state="paused",
                    reason=all_control.reason or "migrated from all=paused",
                    updated_by="migration-0006",
                    row_version=control.c.row_version + 1,
                    updated_at=sa.func.now(),
                )
            )
    bind.execute(control.delete().where(control.c.component == "all"))


def downgrade() -> None:
    control = sa.table(
        "system_control",
        sa.column("component", sa.String()),
        sa.column("state", sa.String()),
    )
    op.get_bind().execute(
        control.update().where(control.c.state == "draining").values(state="paused")
    )
    _replace_state_constraint("'running', 'paused'")
