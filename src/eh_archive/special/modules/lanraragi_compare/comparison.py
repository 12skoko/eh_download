from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

GALLERY_URL = re.compile(
    r"https?://(?:www\.)?(?:exhentai|e-hentai)\.org/g/(\d+)/",
    re.IGNORECASE,
)


def numeric_database_id(manga_id: str) -> int | None:
    value = manga_id.partition("/")[0]
    return int(value) if value.isdigit() else None


def numeric_archive_id(archive: dict[str, Any]) -> int | None:
    tags = archive.get("tags")
    if not isinstance(tags, str):
        return None
    match = GALLERY_URL.search(tags.replace("\\", ""))
    return int(match.group(1)) if match else None


def _sorted_ids(values: Iterable[int]) -> list[int]:
    return sorted(values)


def build_comparison(
    database_manga_ids: Iterable[str], archives: list[dict[str, Any]]
) -> dict[str, Any]:
    database_ids: list[int] = []
    invalid_database_ids: list[str] = []
    for manga_id in database_manga_ids:
        numeric_id = numeric_database_id(manga_id)
        if numeric_id is None:
            invalid_database_ids.append(manga_id)
        else:
            database_ids.append(numeric_id)

    lanraragi_ids: list[int] = []
    unparsed_archives: list[dict[str, str]] = []
    for archive in archives:
        numeric_id = numeric_archive_id(archive)
        if numeric_id is None:
            unparsed_archives.append(
                {
                    "arcid": str(archive.get("arcid", "")),
                    "title": str(archive.get("title", "")),
                }
            )
        else:
            lanraragi_ids.append(numeric_id)

    database_counts = Counter(database_ids)
    lanraragi_counts = Counter(lanraragi_ids)
    database_set = set(database_counts)
    lanraragi_set = set(lanraragi_counts)
    database_only = _sorted_ids(database_set - lanraragi_set)
    lanraragi_only = _sorted_ids(lanraragi_set - database_set)
    return {
        "summary": {
            "database_completed_rows": len(database_ids) + len(invalid_database_ids),
            "database_resolved_ids": len(database_ids),
            "database_unique_ids": len(database_set),
            "lanraragi_archives": len(archives),
            "lanraragi_resolved_ids": len(lanraragi_ids),
            "lanraragi_unique_ids": len(lanraragi_set),
            "database_only": len(database_only),
            "lanraragi_only": len(lanraragi_only),
            "unparsed_lanraragi_archives": len(unparsed_archives),
        },
        "database_only": database_only,
        "lanraragi_only": lanraragi_only,
        "database_duplicate_ids": {
            str(value): count for value, count in sorted(database_counts.items()) if count > 1
        },
        "lanraragi_duplicate_ids": {
            str(value): count for value, count in sorted(lanraragi_counts.items()) if count > 1
        },
        "invalid_database_manga_ids": sorted(invalid_database_ids),
        "unparsed_lanraragi_archives": unparsed_archives,
    }
