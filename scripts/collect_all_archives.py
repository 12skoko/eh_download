from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from eh_archive.config import load_config


def fetch_archives(
    base_url: str,
    *,
    headers: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> list[dict[str, Any]]:
    """Fetch the complete LANraragi archive list."""
    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/api/archives",
            headers=dict(headers or {}),
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"LANraragi request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("LANraragi returned invalid JSON") from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise RuntimeError("LANraragi /api/archives did not return an archive list")
    return payload


def timestamp(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).strftime("%Y%m%d_%H%M%S")


def output_path(log_dir: str | Path, prefix: str, timezone: str) -> Path:
    directory = Path(log_dir) / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / f"{prefix}_{timestamp(timezone)}.json"
    if not base.exists():
        return base
    sequence = 2
    while True:
        candidate = base.with_name(f"{base.stem}_{sequence}.json")
        if not candidate.exists():
            return candidate
        sequence += 1


def write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the complete LANraragi archive list into the configured log directory."
    )
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    app, _, _, secrets = load_config(args.config_dir)
    started = datetime.now(ZoneInfo(app.timezone))
    archives = fetch_archives(
        app.lanraragi_url,
        headers=secrets.lanraragi,
        timeout=args.timeout,
    )
    path = output_path(app.log_dir, "all_archives", app.timezone)
    write_json(path, archives)
    elapsed = (datetime.now(ZoneInfo(app.timezone)) - started).total_seconds()
    print(f"LANraragi archives: {len(archives)}")
    print(f"JSON written: {path.resolve()}")
    print(f"Elapsed: {elapsed:.2f} seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
