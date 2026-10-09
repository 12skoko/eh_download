"""Jinja filters and globals used only by uiv3 templates (prefixed v3_)."""
from __future__ import annotations

import json

from ..services import STATUS_LABELS
from . import common


def register(env, app_config) -> None:
    env.filters["v3_tone"] = common.tone
    env.filters["v3_run_tone"] = common.run_tone
    env.filters["v3_run_label"] = lambda value: common.RUN_LABELS.get(value or "", value or "—")
    env.filters["v3_ago"] = common.ago
    env.filters["v3_clock"] = common.clock
    env.filters["v3_manga_url"] = common.manga_url
    env.filters["v3_json"] = lambda value: json.dumps(value, ensure_ascii=False, indent=2, default=str)
    env.filters["v3_is_mapping"] = lambda value: isinstance(value, dict)
    env.filters["v3_track"] = common.track
    phases = common.phase_labels_by_kind()
    env.filters["v3_phase"] = lambda phase, kind: phases.get(kind, {}).get(phase, phase)
    env.filters["v3_tags"] = lambda raw: [t.strip() for t in (raw or "").replace("，", ",").split(",") if t.strip()]
    from ...special.catalog import MODULES, load_modules

    load_modules()
    kind_labels = {kind: module.label for kind, module in MODULES.items()}
    env.globals.update(
        v3_kind_labels=kind_labels,
        v3_prefix=common.PREFIX,
        v3_asset=common.asset_version(),
        v3_pipeline=common.PIPELINE,
        v3_side_groups=common.SIDE_GROUPS,
        v3_status_groups=common.STATUS_GROUPS,
        v3_status_labels=STATUS_LABELS,
        v3_module_icons=common.MODULE_ICONS,
        v3_kind_icons=common.MODULE_KIND_ICONS,
        v3_timezone=app_config.timezone,
    )
