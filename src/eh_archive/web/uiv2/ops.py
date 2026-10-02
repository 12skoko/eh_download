"""Events, local logs, validated configuration and system management."""
from .bridge import mount
from .common import PREFIX


def install(app, ctx):
    for source, target, name in (
        ("/events", "/events", "events"),
        ("/logs", "/logs", "logs"),
        ("/logs/view", "/logs/view", "log_view"),
        ("/config", "/settings", "settings"),
        ("/config/{section_name}", "/settings/{section_name}", "settings"),
        ("/system", "/system", "system"),
        ("/system/operations/{identifier}", "/system/operations/{identifier}", "system_operation"),
    ):
        mount(app, ctx, source, PREFIX + target, template="uiv2/" + name + ".html",
              partial="uiv2/_events.html" if name == "events" else None)
