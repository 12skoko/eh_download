"""Jinja filters and globals used only by archiveUI templates (prefixed ui_)."""

from __future__ import annotations

import json

from .services import STATUS_LABELS
from . import common, labels


def chart_count(value: int) -> str:
    """Short labels for count axes and bars; tooltips retain the exact count."""
    if value < 10_000:
        return str(value)
    unit, suffix = (100_000_000, "亿") if value >= 100_000_000 else (10_000, "万")
    scaled = value / unit
    number = f"{scaled:.1f}".removesuffix(".0") if scaled < 100 else f"{scaled:.0f}"
    return "1亿" if suffix == "万" and number == "10000" else number + suffix


def register(env, app_config) -> None:
    env.filters["ui_chart_count"] = chart_count
    env.filters["ui_tone"] = common.tone
    env.filters["ui_run_tone"] = common.run_tone
    env.filters["ui_run_label"] = lambda value: common.RUN_LABELS.get(value or "", value or "—")
    timezone = app_config.timezone
    env.filters["ui_ago"] = lambda value: common.ago(value, timezone)
    env.filters["ui_clock"] = lambda value: common.clock(value, timezone)
    env.filters["ui_local"] = lambda value: common.local(value, timezone)
    env.filters["ui_manga_url"] = common.manga_url
    env.filters["ui_json"] = lambda value: json.dumps(
        value, ensure_ascii=False, indent=2, default=str
    )
    env.filters["ui_is_mapping"] = lambda value: isinstance(value, dict)
    env.filters["ui_track"] = common.track
    env.filters["ui_event_type"] = labels.event_type_label
    env.filters["ui_error_code"] = labels.error_code_label
    phases = common.phase_labels_by_kind()
    env.filters["ui_phase"] = lambda phase, kind: phases.get(kind, {}).get(phase, phase)
    env.filters["ui_tags"] = lambda raw: [
        t.strip() for t in (raw or "").replace("，", ",").split(",") if t.strip()
    ]
    from ..special.catalog import MODULES, load_modules

    load_modules()
    kind_labels = {kind: module.label for kind, module in MODULES.items()}
    env.globals.update(
        ui_kind_labels=kind_labels,
        ui_prefix=common.PREFIX,
        ui_asset=common.asset_version(),
        ui_pipeline=common.PIPELINE,
        ui_side_groups=common.SIDE_GROUPS,
        ui_status_groups=common.STATUS_GROUPS,
        ui_status_labels=STATUS_LABELS,
        ui_module_icons=common.MODULE_ICONS,
        ui_kind_icons=common.MODULE_KIND_ICONS,
        ui_timezone=app_config.timezone,
    )
