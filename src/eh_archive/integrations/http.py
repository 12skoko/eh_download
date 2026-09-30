from __future__ import annotations

import time
from typing import Any

from ..config.loader import AppConfig, SecretsConfig, SessionRole
from ..domain.errors import ArchiveError, ErrorClass, classify_exception


class RoleSession:
    """Requests session with explicit account/network role selection."""

    def __init__(
        self,
        app: AppConfig,
        secrets: SecretsConfig,
        *,
        session: Any | None = None,
        request_delay_seconds: float | None = None,
    ) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("requests is required for network integrations") from exc
        self.requests = requests
        self.app, self.secrets = app, secrets
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "EH-Archive/6")
        self.request_delay_seconds = (
            app.external_request_delay_seconds
            if request_delay_seconds is None
            else float(request_delay_seconds)
        )
        self.retry_limit = app.eh_request_retry_limit
        self.retry_delay_seconds = app.eh_request_retry_delay_seconds
        if self.request_delay_seconds < 0:
            raise ValueError("request_delay_seconds must not be negative")
        self._last_request_at: float | None = None

    def _role(self, role: str) -> SessionRole:
        if role == "browse":
            return self.app.browse_session
        if role == "archive":
            return self.app.archive_session
        if role == "full_collect":
            selected = self.app.full_collect_session
            if (not selected.account or selected.account not in self.secrets.accounts
                    or not self.secrets.cookies(selected)):
                raise ValueError("全量采集必须显式选择已配置 Cookie 的账号")
            self.secrets.proxy_pool(selected)
            return selected
        raise ValueError(f"Unknown session role: {role}")

    def request(self, method: str, url: str, *, role: str = "browse", **kwargs: Any) -> Any:
        role_config = self._role(role)
        network_name = kwargs.pop("network_name", None)
        network = (
            self.secrets.proxy_network(role_config, network_name)
            if network_name is not None else self.secrets.network(role_config)
        )
        if "proxy_pool" in network:
            raise ValueError("代理池请求必须显式选择一个固定入口")
        if network_name is not None:
            self.session.trust_env = False
            kwargs["proxies"] = network["proxies"]
            kwargs["allow_redirects"] = False
        retry_enabled = bool(kwargs.pop("_eh_retry", True))
        max_attempts = self.retry_limit if retry_enabled else 1
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            if self._last_request_at is not None and self.request_delay_seconds:
                elapsed = time.monotonic() - self._last_request_at
                remaining = self.request_delay_seconds - elapsed
                if remaining > 0:
                    time.sleep(remaining)
            self.session.cookies.clear()
            self.session.cookies.update(self.secrets.cookies(role_config))
            request_kwargs = dict(kwargs)
            if network.get("proxies") and "proxies" not in request_kwargs:
                request_kwargs["proxies"] = network["proxies"]
            try:
                response = self.session.request(method, url, **request_kwargs)
                if retry_enabled and int(getattr(response, "status_code", 0)) in {
                    408,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    last_error = RuntimeError(
                        f"E-Hentai/ExHentai returned HTTP {response.status_code}"
                    )
                    if attempt >= max_attempts:
                        raise ArchiveError(
                            "eh_site_unavailable",
                            f"E-Hentai/ExHentai unavailable after {attempt} attempts: "
                            f"HTTP {response.status_code}",
                            ErrorClass.SYSTEM,
                        )
                    if self.retry_delay_seconds:
                        time.sleep(self.retry_delay_seconds)
                    continue
                return response
            except ArchiveError:
                raise
            except Exception as exc:
                info = classify_exception(exc)
                if not retry_enabled or info.category != ErrorClass.TEMPORARY:
                    raise
                last_error = exc
                if attempt >= max_attempts:
                    raise ArchiveError(
                        "eh_site_unavailable",
                        f"E-Hentai/ExHentai unavailable after {attempt} attempts: {exc}",
                        ErrorClass.SYSTEM,
                    ) from exc
                if self.retry_delay_seconds:
                    time.sleep(self.retry_delay_seconds)
            finally:
                # Throttle after completion so retries and the next page are
                # also spaced when the previous request fails quickly.
                self._last_request_at = time.monotonic()
        raise AssertionError(f"request loop ended unexpectedly: {last_error}")

    def get(self, url: str, *, role: str = "browse", **kwargs: Any) -> Any:
        return self.request("GET", url, role=role, **kwargs)

    def post(self, url: str, *, role: str = "browse", **kwargs: Any) -> Any:
        return self.request("POST", url, role=role, **kwargs)

    def get_text(
        self, url: str, *, role: str = "browse", timeout: float = 30.0, **kwargs: Any
    ) -> str:
        response = self.request("GET", url, role=role, timeout=timeout, **kwargs)
        response.raise_for_status()
        if 300 <= int(getattr(response, "status_code", 0)) < 400:
            raise ValueError("站点返回重定向，请检查登录状态及采集入口")
        return response.text
