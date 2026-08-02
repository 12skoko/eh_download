from __future__ import annotations

from typing import Any

from ..config.loader import AppConfig, SecretsConfig, SessionRole


class RoleSession:
    """Requests session with explicit browse/archive role selection."""

    def __init__(
        self, app: AppConfig, secrets: SecretsConfig, *, session: Any | None = None
    ) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("requests is required for network integrations") from exc
        self.requests = requests
        self.app, self.secrets = app, secrets
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "EH-Archive/6")

    def _role(self, role: str) -> SessionRole:
        if role == "browse":
            return self.app.browse_session
        if role == "archive":
            return self.app.archive_session
        raise ValueError(f"Unknown session role: {role}")

    def request(self, method: str, url: str, *, role: str = "browse", **kwargs: Any) -> Any:
        role_config = self._role(role)
        self.session.cookies.clear()
        self.session.cookies.update(self.secrets.cookies(role_config))
        network = self.secrets.network(role_config)
        if network.get("proxies") and "proxies" not in kwargs:
            kwargs["proxies"] = network["proxies"]
        return self.session.request(method, url, **kwargs)

    def get(self, url: str, *, role: str = "browse", **kwargs: Any) -> Any:
        return self.request("GET", url, role=role, **kwargs)

    def post(self, url: str, *, role: str = "browse", **kwargs: Any) -> Any:
        return self.request("POST", url, role=role, **kwargs)

    def get_text(self, url: str, *, role: str = "browse", timeout: float = 30.0) -> str:
        response = self.request("GET", url, role=role, timeout=timeout)
        response.raise_for_status()
        return response.text
