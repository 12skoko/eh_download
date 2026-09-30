"""Bounded page batches. All metadata and cursors are committed under one claim."""

import logging
import random
import time
import traceback
from datetime import UTC, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from requests import RequestException
from sqlalchemy import select

from ....config import load_config
from ....db.models import SpecialJob, SpecialWorkflow
from ....db.repository import ArchiveRepository, utcnow
from ....domain.errors import ArchiveError
from ....integrations.http import RoleSession
from ....services.collector.service import manga_record, parse_collection_page
from ...core.contracts import OperationResult
from ...core.execution import ExecutionContext
from .boundaries import deadline, listing_url, page_ids, scope_key
from .config import load_full_collect_config, parse_start_at, validate_listing_url
from .module import OPERATION

log = logging.getLogger(__name__)


class CollectionIssue(ValueError):
    pass


def retry_after(value, now):
    try:
        seconds = float(value)
        if seconds >= 0 and seconds < float("inf"):
            return now + timedelta(seconds=seconds)
    except (ValueError, TypeError, OverflowError):
        pass
    try:
        result = parsedate_to_datetime(value)
        return max(now, result.replace(tzinfo=UTC) if result.tzinfo is None else result)
    except (ValueError, TypeError, OverflowError):
        return now


class FullCollectExecutor:
    def __init__(self, database, *, config_dir, claim):
        self.database, self.config_dir, self.claim = database, config_dir, claim
        self.app, _, _, self.secrets = load_config(config_dir)
        self.config = load_full_collect_config(config_dir, app=self.app, secrets=self.secrets)
        self.context = ExecutionContext(database, claim, config_dir=config_dir, app_config=self.app)
        self.http = RoleSession(self.app, self.secrets, request_delay_seconds=0)
        self.pool = self.secrets.proxy_pool(self.app.full_collect_session)
        self.batch = {"pages": 0, "requests": 0, "created": 0, "updated": 0, "retries": 0}

    def _transaction(self):
        return self.context.transaction(timeout_seconds=self.config.page_write_timeout_seconds)

    def _snapshot(self):
        with self._transaction() as repository:
            workflow = repository.session.get(SpecialWorkflow, self.claim.workflow_id)
            return dict(workflow.payload)

    def _log(self, event, **values):
        log.info(
            "full_collect %s workflow=%s job=%s %s",
            event,
            self.claim.workflow_id,
            self.claim.job_id,
            values,
            extra={
                "event": {
                    "name": event,
                    "workflow_id": self.claim.workflow_id,
                    "job_id": self.claim.job_id,
                    **values,
                }
            },
        )

    def _begin_request(self, data):
        now = utcnow()
        index = (
            now.astimezone(ZoneInfo(self.app.timezone)).hour + data.get("proxy_offset", 0)
        ) % len(self.pool)
        network = self.pool[index]
        with self._transaction() as repository:
            workflow = repository.session.get(SpecialWorkflow, self.claim.workflow_id)
            data = dict(workflow.payload)
            if data.get("intent") != "run":
                return None
            previous = data.get("network", {}).get("name")
            data["batch_budget"] = {
                "max_pages": self.config.batch_max_pages,
                "max_seconds": self.config.batch_max_seconds,
            }
            data["network"] = {
                "account": self.app.full_collect_session.account,
                "name": network,
                "index": index + 1,
                "pool_size": len(self.pool),
                "reason": "error_rotation" if data.get("request_failures") else "hourly",
            }
            # If killed inside HTTP, preserve conservative pacing before retry.
            data["next_request_at"] = (
                now
                + timedelta(
                    seconds=2 * self.config.request_timeout_seconds
                    + self.config.page_delay_max_seconds,
                )
            ).isoformat()
            data["counts"] = {**data["counts"], "requests": data["counts"]["requests"] + 1}
            data["started_at"] = data.get("started_at") or now.isoformat()
            if not repository.update_state(
                self.claim,
                payload=data,
                progress={
                    "message": "requesting",
                    "batch": dict(self.batch),
                },
            ):
                raise RuntimeError("stale full collection request")
        self.batch["requests"] += 1
        if previous != network:
            self._log("proxy_selected", **data["network"])
        return network

    def _received(self, ended):
        until = ended + timedelta(
            seconds=random.uniform(
                self.config.page_delay_min_seconds,
                self.config.page_delay_max_seconds,
            )
        )
        with self._transaction() as repository:
            workflow = repository.session.get(SpecialWorkflow, self.claim.workflow_id)
            data = {**workflow.payload, "next_request_at": until.isoformat()}
            if not repository.update_state(self.claim, payload=data):
                raise RuntimeError("stale full collection request completion")

    def _finish(self, phase, reason):
        next_job, selected_delay, due = None, None, None
        with self._transaction() as repository:
            workflow = repository.session.get(SpecialWorkflow, self.claim.workflow_id)
            data = dict(workflow.payload)
            if data.get("end_reached"):
                phase, reason = "completed", "range_end"
            elif data.get("intent") != "run":
                phase, reason = "paused", "user_pause"
            data["stop_reason"], data["last_batch"] = reason, dict(self.batch)
            operation = OPERATION if phase in {"queued", "cooling"} else None
            if operation:
                selected_delay = random.uniform(
                    self.config.job_delay_min_seconds, self.config.job_delay_max_seconds
                )
                data["batch_not_before"] = (
                    utcnow() + timedelta(seconds=selected_delay)
                ).isoformat()
                due = deadline(data)
            result = OperationResult(
                phase,
                payload=data,
                progress={"message": phase, "batch": dict(self.batch)},
                status="completed" if phase == "completed" else None,
                next_operation=operation,
                delay_seconds=max(0, (due - utcnow()).total_seconds()) if due else 0,
            )
            if not repository.commit_result(self.claim, result):
                raise RuntimeError("stale full collection batch result")
            if operation:
                job = repository.session.scalar(
                    select(SpecialJob).where(
                        SpecialJob.workflow_id == self.claim.workflow_id,
                        SpecialJob.status == "queued",
                    )
                )
                next_job, due = job.id, job.next_run_at
        self._log(
            "batch_finished",
            phase=phase,
            reason=reason,
            counts=self.batch,
            job_delay=selected_delay,
            next_job_id=next_job,
            next_run_at=due.isoformat() if due else None,
        )

    def _issue(self, code, *, temporary=False, cooldown=None, frames=None):
        with self._transaction() as repository:
            workflow = repository.session.get(SpecialWorkflow, self.claim.workflow_id)
            data = dict(workflow.payload)
            data["last_error"] = {"code": code, "at": utcnow().isoformat()}
            data["counts"] = {
                **data["counts"],
                "retries": data["counts"]["retries"] + int(temporary),
            }
            if temporary:
                data["proxy_offset"] = data.get("proxy_offset", 0) + 1
                data["request_failures"] = data.get("request_failures", 0) + 1
                data["next_request_at"] = max(
                    parse_start_at(data["next_request_at"]),
                    utcnow() + timedelta(seconds=self.config.retry_delay_seconds),
                ).isoformat()
            pool_failed = temporary and data["request_failures"] >= len(self.pool)
            cooling = bool(cooldown) or pool_failed
            if cooling:
                data["consecutive_failures"] = data.get("consecutive_failures", 0) + 1
                data["request_failures"] = 0
                data["cooldown_until"] = max(
                    cooldown or utcnow(),
                    utcnow() + timedelta(seconds=self.config.pool_failure_cooldown_seconds),
                ).isoformat()
            exhausted = data.get("consecutive_failures", 0) >= self.config.max_consecutive_failures
            phase = (
                "waiting_repair" if not temporary or exhausted else "cooling" if cooling else None
            )
            if not repository.update_state(
                self.claim,
                payload=data,
                progress={
                    "message": phase or "retrying",
                    "error": code,
                    "batch": dict(self.batch),
                },
            ):
                raise RuntimeError("stale full collection error")
        self.batch["retries"] += int(temporary)
        # Network exception strings can contain proxy credentials: only emit a
        # controlled error code and source locations, never request objects/text.
        log.warning(
            "full_collect request_issue workflow=%s job=%s code=%s phase=%s stack=%s",
            self.claim.workflow_id,
            self.claim.job_id,
            code,
            phase,
            frames or [],
        )
        if phase:
            self._finish(phase, code)
            return True
        return False

    def _commit_page(self, page, *, elapsed_seconds=0):
        write_started = time.monotonic()
        try:
            ids = page_ids(page)
            next_url = None
            if page.next_url:
                validated = validate_listing_url(page.next_url, base_url=self.config.base_url)
                cursor = {
                    k: v
                    for k, v in parse_qsl(urlsplit(validated).query)
                    if k in {"next", "prev", "seek"}
                }
                if "next" not in cursor:
                    raise ValueError("下一页缺少站点 next 游标")
                next_url = listing_url(validated, **cursor)
        except ValueError as exc:
            raise CollectionIssue(str(exc)) from None
        with self._transaction() as repository:
            workflow = repository.session.get(SpecialWorkflow, self.claim.workflow_id)
            data = dict(workflow.payload)
            if data.get("intent") != "run":
                return False
            upper = parse_start_at(data["scope"]["upper_at"])
            if not data["boundary_verified"]:
                newer = any(item.posted_at > upper for item in page.items)
                if not newer and not page.first_page:
                    if not page.prev_url or data.get("positioning_pages", 0) >= 100:
                        raise CollectionIssue("无法验证日期起点，请检查页面或使用手工分页 URL")
                    try:
                        previous = validate_listing_url(
                            page.prev_url, base_url=data["scope"]["base_url"]
                        )
                    except ValueError:
                        raise CollectionIssue("起点回溯链接无效") from None
                    if previous in data.get("positioning_urls", []):
                        raise CollectionIssue("起点回溯出现循环")
                    data["positioning_urls"] = (data.get("positioning_urls", []) + [page.url])[-4:]
                    data["positioning_pages"] = data.get("positioning_pages", 0) + 1
                    data["cursor"] = previous
                    if not repository.update_state(self.claim, payload=data, phase="positioning"):
                        raise RuntimeError("stale full collection positioning")
                    return True
                data["boundary_verified"] = True
            last_gid = data.get("scan_last_gid")
            if ids and last_gid is not None and (ids[0] > last_gid or ids[-1] >= last_gid):
                raise CollectionIssue("下一页没有向历史推进，保留原游标等待核实")
            if next_url and next_url in (data.get("recent_urls", []) + [page.url]):
                raise CollectionIssue("列表分页出现循环")
            eligible = [item for item in page.items if item.posted_at <= upper]
            if not data.get("initialized") and eligible:
                item = eligible[0]
                anchor = {
                    "gid": int(item.manga_id.split("/", 1)[0]),
                    "manga_id": item.manga_id,
                    "target_at": item.posted_at.isoformat(),
                    "page_url": page.url,
                }
                if data.get("lower_anchor") and anchor["gid"] <= data["lower_anchor"]["gid"]:
                    raise CollectionIssue("新起点没有越过旧覆盖边界，请重新核实补齐起点")
                data.update(initialized=True, upper_anchor=anchor)
            if not next_url and not data.get("initialized"):
                raise CollectionIssue("起点未找到范围内档案，不能将空页记作全量完成")
            created = updated = 0
            archive = ArchiveRepository(repository.session)
            for item in eligible:
                if time.monotonic() - write_started > self.config.page_write_timeout_seconds:
                    raise TimeoutError("full collection page transaction budget exceeded")
                record = manga_record(item)
                record.status = "filtered_out"
                record.remark = "[full_collect] 全量收集建档"
                record.queue_source = "automatic"
                _, added = archive.upsert_manga_metadata(record)
                created += int(added)
                updated += int(not added)
            # Only numeric order verified above can establish crossing, not a
            # coincidental existing database ID or a removed boundary record.
            below = data.get("lower_anchor") and ids and ids[0] < data["lower_anchor"]["gid"]
            overlap = data.get("overlap_pages", 0) + 1 if below else 0
            data["overlap_pages"] = overlap
            if (
                data.get("lower_anchor")
                and not next_url
                and (not ids or ids[-1] > data["lower_anchor"]["gid"])
            ):
                raise CollectionIssue("末页尚未越过补齐旧边界，不能声称区间完整")
            terminal = not next_url or (below and overlap >= self.config.boundary_overlap_pages)
            data["end_reached"], data["cursor"] = bool(terminal), next_url
            data["recent_urls"] = (data.get("recent_urls", []) + [page.url])[-4:]
            if ids:
                data["scan_last_gid"] = ids[-1]
            data["counts"] = {
                **data["counts"],
                "pages": data["counts"]["pages"] + 1,
                "found": data["counts"]["found"] + len(eligible),
                "created": data["counts"]["created"] + created,
                "updated": data["counts"]["updated"] + updated,
            }
            data.update(
                consecutive_failures=0, request_failures=0, cooldown_until="", last_error=None
            )
            data["last_page"] = {
                "url": page.url,
                "committed_at": utcnow().isoformat(),
                "first_gid": ids[0] if ids else None,
                "last_gid": ids[-1] if ids else None,
                "oldest_at": min((x.posted_at for x in page.items), default=upper).isoformat(),
                "newest_at": max((x.posted_at for x in page.items), default=upper).isoformat(),
                "found": len(eligible),
                "created": created,
                "updated": updated,
                "request_seconds": round(elapsed_seconds, 3),
            }
            if time.monotonic() - write_started > self.config.page_write_timeout_seconds:
                raise TimeoutError("full collection page transaction budget exceeded")
            batch = {
                **self.batch,
                "pages": self.batch["pages"] + 1,
                "created": self.batch["created"] + created,
                "updated": self.batch["updated"] + updated,
            }
            if not repository.update_state(
                self.claim,
                payload=data,
                phase="collecting",
                progress={
                    "message": "page_committed",
                    "batch": batch,
                },
            ):
                raise RuntimeError("stale full collection page")
        self.batch = batch
        self._log("page_committed", **data["last_page"], next_url=next_url)
        return True

    def run(self):
        started = time.monotonic()
        attempts = 0
        self._log(
            "batch_started",
            max_pages=self.config.batch_max_pages,
            max_seconds=self.config.batch_max_seconds,
        )
        try:
            while True:
                data = self._snapshot()
                if data.get("end_reached"):
                    return self._finish("completed", "range_end")
                if data.get("intent") != "run":
                    return self._finish("paused", "user_pause")
                if reason := self.context.stop_requested():
                    return self._finish("interrupted", reason)
                if data["scope"]["fingerprint"] != scope_key(
                    self.config.base_url,
                    self.app.full_collect_session.account,
                ):
                    return self._issue("scope_configuration_changed")
                if (
                    attempts >= self.config.batch_max_pages
                    or time.monotonic() - started >= self.config.batch_max_seconds
                ):
                    return self._finish("queued", "batch_budget")
                remaining = (deadline(data) - utcnow()).total_seconds()
                if remaining > 0:
                    time.sleep(min(remaining, self.config.control_poll_seconds))
                    continue
                network = self._begin_request(data)
                if network is None:
                    return self._finish("paused", "user_pause")
                attempts += 1
                response, failure = None, None
                began = time.monotonic()
                try:
                    response = self.http.get(
                        data["cursor"],
                        role="full_collect",
                        network_name=network,
                        _eh_retry=False,
                        allow_redirects=False,
                        timeout=(
                            self.config.request_timeout_seconds,
                            self.config.request_timeout_seconds,
                        ),
                    )
                except RequestException as exc:
                    failure = exc
                finally:
                    self._received(utcnow())
                if failure is not None:
                    frames = [
                        f"{frame.name}:{frame.lineno}"
                        for frame in traceback.extract_tb(failure.__traceback__)
                    ]
                    if self._issue("network_request_failed", temporary=True, frames=frames):
                        return
                    continue
                status = response.status_code
                if status in {429, 509}:
                    if self._issue(
                        "http_rate_limited",
                        temporary=True,
                        cooldown=retry_after(response.headers.get("Retry-After", ""), utcnow()),
                    ):
                        return
                    continue
                if status in {408, 425, 500, 502, 503, 504}:
                    supplied = response.headers.get("Retry-After")
                    if self._issue(
                        f"http_{status}",
                        temporary=True,
                        cooldown=retry_after(supplied, utcnow()) if supplied else None,
                    ):
                        return
                    continue
                if status != 200:
                    return self._issue(f"http_{status}_requires_repair")
                try:
                    page = parse_collection_page(response.text, data["cursor"])
                    if self.context.stop_requested():
                        return self._finish("interrupted", "supervisor_stop")
                    if not self._commit_page(page, elapsed_seconds=time.monotonic() - began):
                        return self._finish("paused", "user_pause")
                except (ArchiveError, CollectionIssue) as exc:
                    code = exc.info.code if isinstance(exc, ArchiveError) else str(exc)
                    return self._issue(code)
        finally:
            self.http.session.close()
