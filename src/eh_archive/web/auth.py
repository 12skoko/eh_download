from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

PASSWORD_ALGORITHM = "scrypt"
SESSION_COOKIE = "eharchive_session"
SESSION_MAX_AGE_SECONDS = 24 * 60 * 60


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return "$".join(
        (
            PASSWORD_ALGORITHM,
            str(n),
            str(r),
            str(p),
            _encode(salt),
            _encode(digest),
        )
    )


def verify_password(password: str, encoded: str | None) -> bool:
    parsed = _password_parts(encoded)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def valid_password_hash(encoded: str | None) -> bool:
    return _password_parts(encoded) is not None


@dataclass(frozen=True)
class WebIdentity:
    username: str
    csrf_token: str
    expires_at: int


class SessionSigner:
    def __init__(self, secret: str, *, max_age: int = SESSION_MAX_AGE_SECONDS) -> None:
        if len(secret) < 16:
            raise ValueError("web_secret must contain at least 16 characters")
        self.key = secret.encode("utf-8")
        self.max_age = max_age

    def create(self, username: str, *, now: int | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = {
            "sub": username,
            "iat": issued_at,
            "exp": issued_at + self.max_age,
            "csrf": secrets.token_urlsafe(24),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self.key, body, hashlib.sha256).digest()
        return f"{_encode(body)}.{_encode(signature)}"

    def verify(self, token: str | None, *, now: int | None = None) -> WebIdentity | None:
        if not token:
            return None
        try:
            body_value, signature_value = token.split(".", 1)
            body = _decode(body_value)
            signature = _decode(signature_value)
            expected = hmac.new(self.key, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return None
            payload: dict[str, Any] = json.loads(body)
            current = int(time.time() if now is None else now)
            username = str(payload["sub"])
            csrf_token = str(payload["csrf"])
            expires_at = int(payload["exp"])
            issued_at = int(payload["iat"])
            if not username or not csrf_token or issued_at > current + 60 or expires_at < current:
                return None
            if expires_at - issued_at > self.max_age:
                return None
            return WebIdentity(username, csrf_token, expires_at)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _password_parts(encoded: str | None) -> tuple[int, int, int, bytes, bytes] | None:
    if not encoded:
        return None
    try:
        algorithm, n_value, r_value, p_value, salt_value, expected_value = encoded.split("$", 5)
        n, r, p = int(n_value), int(r_value), int(p_value)
        salt, expected = _decode(salt_value), _decode(expected_value)
        if (
            algorithm != PASSWORD_ALGORITHM
            or (n, r, p) != (2**14, 8, 1)
            or len(salt) != 16
            or len(expected) != 32
        ):
            return None
        return n, r, p, salt, expected
    except (TypeError, ValueError):
        return None
