from __future__ import annotations

import stat
from contextlib import suppress
from pathlib import PureWindowsPath
from types import TracebackType
from typing import Any, BinaryIO, Self


class SmbStore:
    """Small, credential-safe wrapper around smbprotocol's high-level smbclient API."""

    def __init__(
        self,
        *,
        server: str,
        share: str,
        relative_dir: str,
        username: str,
        password: str,
        port: int = 445,
        connection_timeout: float = 60.0,
        encrypt: bool = False,
    ) -> None:
        self.server = server.strip().strip("\\/")
        self.share = share.strip().strip("\\/")
        self.relative_dir = relative_dir.replace("/", "\\").strip("\\")
        self.username = username
        self.password = password
        self.port = port
        self.connection_timeout = connection_timeout
        self.encrypt = encrypt
        self._client: Any | None = None
        self._connected = False
        self._validate_config()

    def _validate_config(self) -> None:
        if not self.server:
            raise ValueError("lanraragi_smb_server must not be empty")
        if not self.share or "\\" in self.share or "/" in self.share:
            raise ValueError("lanraragi_smb_share must be one SMB share name")
        if not self.username:
            raise ValueError("lanraragi_smb username must not be empty")
        if not self.password:
            raise ValueError("lanraragi_smb password must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("lanraragi_smb_port must be between 1 and 65535")
        if self.connection_timeout <= 0:
            raise ValueError("lanraragi_smb_connection_timeout_seconds must be positive")
        parts = PureWindowsPath(self.relative_dir).parts
        if any(part in {".", ".."} for part in parts):
            raise ValueError("lanraragi_smb_relative_dir must not contain dot segments")

    @property
    def root(self) -> str:
        value = f"\\\\{self.server}\\{self.share}"
        return f"{value}\\{self.relative_dir}" if self.relative_dir else value

    def path(self, filename: str) -> str:
        if not filename or filename in {".", ".."} or any(char in filename for char in "\\/"):
            raise ValueError("remote filename must be an unchanged basename")
        return f"{self.root}\\{filename}"

    def connect(self) -> None:
        if self._connected:
            return
        try:
            import smbclient
        except ImportError as exc:
            raise RuntimeError("smbprotocol is required for filesystem uploads") from exc
        self._client = smbclient
        smbclient.register_session(
            self.server,
            username=self.username,
            password=self.password,
            port=self.port,
            encrypt=self.encrypt,
            connection_timeout=self.connection_timeout,
            auth_protocol="negotiate",
            require_signing=True,
        )
        self._connected = True
        try:
            target = smbclient.stat(self.root)
            if not stat.S_ISDIR(target.st_mode):
                raise NotADirectoryError("configured LANraragi SMB target is not a directory")
        except Exception:
            with suppress(Exception):
                self.close()
            raise

    def close(self) -> None:
        client, self._client = self._client, None
        was_connected, self._connected = self._connected, False
        if client is not None and was_connected:
            client.delete_session(self.server, port=self.port)

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except Exception:
            if exc is None:
                raise

    def exists(self, filename: str) -> bool:
        return bool(self._require_client().path.exists(self.path(filename)))

    def size(self, filename: str) -> int:
        return int(self._require_client().stat(self.path(filename)).st_size)

    def open_write(self, filename: str) -> BinaryIO:
        return self._require_client().open_file(self.path(filename), mode="xb")

    def open_read(self, filename: str) -> BinaryIO:
        return self._require_client().open_file(self.path(filename), mode="rb")

    def remove(self, filename: str) -> None:
        self._require_client().remove(self.path(filename))

    def rename(self, source: str, destination: str) -> None:
        # smbclient.rename hard-codes replace_if_exists=False in its internal
        # FileRenameInformation request.
        self._require_client().rename(self.path(source), self.path(destination))

    def _require_client(self) -> Any:
        if not self._connected or self._client is None:
            raise RuntimeError("SMB session is not connected")
        return self._client
