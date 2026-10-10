"""Jinja filters and globals used only by archiveUI templates (prefixed ui_)."""

from __future__ import annotations

import json

from .services import STATUS_LABELS
from . import common


def register(env, app_config) -> None:
    env.filters["ui_tone"] = common.tone
    env.filters["ui_run_tone"] = common.run_tone
    env.filters["ui_run_label"] = lambda value: common.RUN_LABELS.get(value or "", value or "—")
    env.filters["ui_ago"] = common.ago
    env.filters["ui_clock"] = common.clock
    env.filters["ui_manga_url"] = common.manga_url
    env.filters["ui_json"] = lambda value: json.dumps(
        value, ensure_ascii=False, indent=2, default=str
    )
    env.filters["ui_is_mapping"] = lambda value: isinstance(value, dict)
    env.filters["ui_track"] = common.track
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
