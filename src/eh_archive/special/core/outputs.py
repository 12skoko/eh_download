"""Immutable per-attempt files, published before fenced manifest registration."""

import json
import os
import re
from pathlib import Path

from ...db.repository import utcnow


def output_path(root, storage_key):
    root = Path(root).resolve()
    key = Path(storage_key)
    if key.is_absolute() or key.drive or ".." in key.parts or "\\" in storage_key:
        raise ValueError("invalid output reference")
    path = (root / key).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("output reference escapes storage")
    return path


def publish_json(root, claim, output_id, data, *, name="report.json"):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", output_id):
        raise ValueError("invalid output ID")
    key = f"{claim.workflow_id}/{claim.job_id}/{claim.lease_token}/{output_id}.json"
    path = output_path(root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    return {
        "id": output_id,
        "job_id": claim.job_id,
        "name": name,
        "media_type": "application/json",
        "size_bytes": path.stat().st_size,
        "storage_key": key,
        "created_at": utcnow().isoformat(),
    }


def register_output(repository, claim, entry):
    values = repository._values(claim)
    if not values:
        return False
    job, workflow = values
    repository._check_cancel(workflow, job)
    if (
        entry.get("job_id") != job.id
        or not isinstance(entry.get("size_bytes"), int)
        or entry["size_bytes"] < 0
        or not re.fullmatch(r"[a-zA-Z0-9_-]+", entry.get("id", ""))
        or set(entry)
        != {"id", "job_id", "name", "media_type", "size_bytes", "storage_key", "created_at"}
    ):
        raise ValueError("invalid output manifest")
    if not entry["storage_key"].startswith(f"{workflow.id}/{job.id}/{claim.lease_token}/"):
        raise ValueError("output belongs to another attempt")
    outputs = {e["id"]: e for e in (workflow.payload or {}).get("outputs", [])}
    outputs[entry["id"]] = dict(entry)
    workflow.payload = {**(workflow.payload or {}), "outputs": list(outputs.values())}
    return True
