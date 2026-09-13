from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleCapability:
    kind: str
    enabled: bool
    max_concurrency: int
    reason: str | None = None


def module_capability(kind, config_dir):
    from .catalog import MODULES, load_modules

    load_modules()
    module = MODULES.get(kind)
    if module is None:
        return ModuleCapability(kind, False, 0, "特殊处理模块未安装")
    try:
        return module.capability(config_dir)
    except (OSError, TypeError, ValueError):
        return ModuleCapability(kind, False, 0, "模块配置无效，请检查配置文件")


def enabled_module_capabilities(config_dir):
    from .catalog import MODULES, load_modules

    load_modules()
    return tuple(c for k in MODULES if (c := module_capability(k, config_dir)).enabled)


def build_executor(kind, database, *, config_dir, claim):
    from .catalog import MODULES, load_modules

    load_modules()
    if not module_capability(kind, config_dir).enabled:
        raise ValueError("特殊处理模块已禁用或配置无效")
    return MODULES[kind].executor(database, config_dir, claim)
