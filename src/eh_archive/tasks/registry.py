"""Ordinary modules share one scheduler and runner; extensions remain separate."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    label: str
    handler: str = "eh_archive.tasks.runner:run_records"
    schedule: Literal["condition", "interval"] = "condition"
    initial_delay_seconds: float = 0.0
    interval_seconds: float = 0.0
    # Timed record sweeps visit each eligible record at most once per run.
    sweep: bool = False
    legacy_initial_delay_setting: str | None = None
    legacy_interval_setting: str | None = None


MODULES = {
    module.name: module
    for module in (
        ModuleSpec(
            "collect",
            "采集",
            "eh_archive.tasks.collect:run",
            "interval",
            60.0,
            10800.0,
            legacy_initial_delay_setting="collect_initial_delay_seconds",
            legacy_interval_setting="collect_interval_seconds",
        ),
        ModuleSpec("screen", "筛选", "eh_archive.tasks.screen:run"),
        ModuleSpec("details", "详情补全"),
        ModuleSpec("torrent_download", "种子提交"),
        ModuleSpec(
            "torrent_check",
            "种子完成检测",
            schedule="interval",
            initial_delay_seconds=60.0,
            interval_seconds=60.0,
            sweep=True,
            legacy_interval_setting="torrent_poll_seconds",
        ),
        ModuleSpec("direct_download", "直接下载"),
        ModuleSpec("validate", "校验"),
        ModuleSpec("prepare", "压缩准备"),
        ModuleSpec("upload", "上传"),
        ModuleSpec("cleanup", "清理"),
        ModuleSpec("delete", "档案删除"),
    )
}
