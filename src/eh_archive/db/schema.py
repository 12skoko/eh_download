from __future__ import annotations

from pathlib import Path

from .session import Database


def upgrade(database: Database) -> None:
    """Apply Alembic revisions, falling back to create-all for embedded use."""

    project_config = Path(__file__).resolve().parents[3] / "alembic.ini"
    config_path = Path("alembic.ini")
    if not config_path.exists() and project_config.exists():
        config_path = project_config
    if not config_path.exists():
        database.create_schema()
        return
    from alembic import command
    from alembic.config import Config

    alembic_config = Config(str(config_path))
    with database.engine.begin() as connection:
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, "head")
