from ..special.catalog import MODULES, load_modules
from ..special.service import special_module_health


def get_special_module_page(kind):
    load_modules()
    try:
        return MODULES[kind]
    except KeyError as exc:
        raise ValueError("unsupported special module page") from exc


def special_module_cards(config_dir):
    load_modules()
    return tuple(
        {
            "kind": page.kind,
            "label": page.label,
            "description": page.description,
            "url": page.url,
            "health": special_module_health(page.kind, config_dir),
        }
        for page in MODULES.values()
    )


def special_module_url(kind):
    load_modules()
    return MODULES[kind].url if kind in MODULES else "/special"
