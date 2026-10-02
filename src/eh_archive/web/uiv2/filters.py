"""Jinja filters and globals used only by uiv2 templates (prefixed v2_)."""
from __future__ import annotations

import json

from ..services import STATUS_LABELS
from . import common


def register(env, app_config) -> None:
    env.filters["v2_tone"] = common.tone
    env.filters["v2_run_tone"] = common.run_tone
    env.filters["v2_run_label"] = lambda value: common.RUN_LABELS.get(value or "", value or "—")
    env.filters["v2_ago"] = common.ago
    env.filters["v2_clock"] = common.clock
    env.filters["v2_manga_url"] = common.manga_url
    env.filters["v2_json"] = lambda value: json.dumps(value, ensure_ascii=False, indent=2, default=str)
    env.filters["v2_is_mapping"] = lambda value: isinstance(value, dict)
    env.filters["v2_track"] = common.track
    phases = common.phase_labels_by_kind()
    env.filters["v2_phase"] = lambda phase, kind: phases.get(kind, {}).get(phase, phase)
    env.filters["v2_tags"] = lambda raw: [t.strip() for t in (raw or "").replace("，", ",").split(",") if t.strip()]
    from ...special.catalog import MODULES, load_modules

    load_modules()
    kind_labels = {kind: module.label for kind, module in MODULES.items()}
    env.globals.update(
        v2_kind_labels=kind_labels,
        v2_prefix=common.PREFIX,
        v2_asset=common.asset_version(),
        v2_pipeline=common.PIPELINE,
        v2_side_groups=common.SIDE_GROUPS,
        v2_status_groups=common.STATUS_GROUPS,
        v2_status_labels=STATUS_LABELS,
        v2_module_icons=common.MODULE_ICONS,
        v2_kind_icons=common.MODULE_KIND_ICONS,
        v2_timezone=app_config.timezone,
    )
