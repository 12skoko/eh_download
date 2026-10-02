"""EH Archive console v2, mounted under /uiv2 next to the original interface."""
from __future__ import annotations

from pathlib import Path

from .common import PREFIX, Uiv2


def install_uiv2(
    app,
    *,
    templates,
    database,
    app_config,
    supervisor_config,
    secrets_config,
    signer,
    auth_enabled: bool,
    config_dir: Path,
    management_path: Path,
) -> Uiv2:
    from . import archives, core, filters, ops, workflows

    ctx = Uiv2(
        templates=templates, database=database, app_config=app_config,
        supervisor_config=supervisor_config, secrets_config=secrets_config, signer=signer,
        auth_enabled=auth_enabled, config_dir=Path(config_dir),
        management_path=Path(management_path),
    )
    filters.register(templates.env, app_config)
    core.install(app, ctx)
    archives.install(app, ctx)
    workflows.install(app, ctx)
    ops.install(app, ctx)
    return ctx


__all__ = ["PREFIX", "install_uiv2"]
