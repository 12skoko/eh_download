from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL


def _required_text(section: dict[str, Any], name: str, section_name: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[{section_name}] {name} must be a non-empty string")
    return value


def _database_url(
    raw: dict[str, Any], *, section_name: str, drivername: str, default_port: int
) -> URL:
    section = raw.get(section_name)
    if not isinstance(section, dict):
        raise TypeError(f"missing [{section_name}] section")
    host = _required_text(section, "host", section_name)
    username = _required_text(section, "username", section_name)
    database = _required_text(section, "database", section_name)
    password = section.get("password", "")
    if not isinstance(password, str):
        raise TypeError(f"[{section_name}] password must be a string")
    port = section.get("port", default_port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"[{section_name}] port must be an integer between 1 and 65535")
    return URL.create(
        drivername,
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
    )


def load_migration_config(path: str | Path) -> tuple[URL, URL]:
    config_path = Path(path)
    if not config_path.exists():
        raise ValueError(f"migration config does not exist: {config_path}")
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in migration config: {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TypeError("migration config must contain TOML tables")
    mysql = _database_url(
        raw, section_name="mysql", drivername="mysql+pymysql", default_port=3306
    )
    postgres = _database_url(
        raw, section_name="postgres", drivername="postgresql+psycopg", default_port=5432
    )
    return mysql, postgres
