"""Shared presentation and validation for owner-bound interval controls."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..db.models import EventLog, SystemControl
from ..tasks.registry import MODULES

INTERVAL_MODULES = tuple(name for name, spec in MODULES.items() if spec.schedule == "interval")
ACTIVE_REQUESTS = {"pending", "starting"}


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def in_maintenance(config, timezone, now):
    start, end = config.maintenance_start, config.maintenance_end
    if start is None or end is None:
        return False
    current = now.astimezone(ZoneInfo(timezone)).time().replace(tzinfo=None)
    return start <= current < end if start < end else current >= start or current < end


def schedule_view(row, supervisor, config, timezone, *, now=None):
    now = now or datetime.now(UTC)
    max_age = max(config.poll_seconds * 6, 30)
    online = bool(
        supervisor and supervisor.lease_owner and supervisor.lease_until
        and aware(supervisor.lease_until) > now and supervisor.heartbeat_at
        and (now - aware(supervisor.heartbeat_at)).total_seconds() <= max_age
    )
    current = bool(
        online and row and row.lease_owner == supervisor.lease_owner
        and row.schedule_updated_at and row.next_run_at
        and (now - aware(row.schedule_updated_at)).total_seconds() <= max_age
    )
    reason = None
    if not online:
        reason = "Supervisor 离线"
    elif not current:
        reason = "等待调度信息更新"
    elif not config.modules.get(row.component, False):
        reason = "模块已禁用"
    elif supervisor.state != "running":
        reason = "Supervisor 已暂停" if supervisor.state == "paused" else "Supervisor 正在停止调度"
    elif row.state != "running":
        reason = "模块已暂停"
    elif in_maintenance(config, timezone, now):
        reason = "维护时段内，暂不可执行"
    elif row.schedule_running:
        reason = "模块运行中"
    elif row.schedule_block_reason:
        reason = row.schedule_block_reason
    elif row.trigger_status in ACTIVE_REQUESTS:
        reason = "等待 Supervisor 接收" if row.trigger_status == "pending" else "正在启动"
    feedback = row.trigger_message if row else None
    if (
        row and row.trigger_status in ACTIVE_REQUESTS and supervisor
        and supervisor.lease_owner and row.trigger_owner != supervisor.lease_owner
    ):
        feedback = "请求已失效：Supervisor 已重启，请重新触发"
    return {
        "owner": supervisor.lease_owner if online else "",
        "next_run_at": aware(row.next_run_at) if current else None,
        "unavailable": reason,
        "feedback": feedback,
        "request_version": row.trigger_requested_at.isoformat()
        if row and row.trigger_requested_at else "",
    }


def request_run(session, component, *, owner, request_version, actor, config, timezone):
    if component not in INTERVAL_MODULES:
        raise ValueError("此模块不支持手动运行")
    # All schedule writers lock the Supervisor first, then the module.
    supervisor = session.get(SystemControl, "supervisor", with_for_update=True)
    row = session.get(SystemControl, component, with_for_update=True)
    view = schedule_view(row, supervisor, config, timezone)
    if not owner or owner != view["owner"]:
        raise ValueError("Supervisor 已变化或离线，请刷新后重试")
    if view["unavailable"]:
        raise ValueError(view["unavailable"])
    if request_version != view["request_version"]:
        raise ValueError("请求状态已变化，请刷新后重试")
    row.trigger_requested_at = datetime.now(UTC)
    row.trigger_owner = owner
    row.trigger_status = "pending"
    row.trigger_message = "已提交，等待 Supervisor 接收"
    session.add(EventLog(
        component="web", event_type="manual", operation="module_trigger", actor=actor,
        detail={"component": component, "supervisor_owner": owner},
    ))
