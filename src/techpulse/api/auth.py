"""Read-only authentication, request ids, rate limits, and signed cursors.

Read tokens and the cursor HMAC key are taken from the process environment
when the process starts. There is no baked-in key. This module does not open
an env file and does not call a network.

``/metrics`` trusts only the direct client address. Forwarded headers are
ignored so a caller cannot mark itself internal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import cast
from uuid import uuid4

from fastapi import Request

from techpulse.workflows.activities import ObjectStore

SCHEMA_VERSION = "1.0"
ENV_READ_TOKENS = "TECHPULSE_API_READ_TOKENS"
ENV_CURSOR_KEY = "TECHPULSE_CURSOR_HMAC_KEY"
ENV_ANON_LIMIT = "TECHPULSE_ANON_LIMIT"
ENV_RATE_LIMIT = "TECHPULSE_RATE_LIMIT"
ENV_RATE_WINDOW = "TECHPULSE_RATE_WINDOW_SECONDS"
ENV_CURSOR_TTL = "TECHPULSE_CURSOR_TTL_SECONDS"
ENV_CORS = "TECHPULSE_CORS_ORIGINS"
ENV_DATABASE = "TECHPULSE_API_DATABASE"
ENV_ARTIFACTS = "TECHPULSE_ARTIFACTS_DIR"

_DEFAULT_ANON_LIMIT = 30
_DEFAULT_RATE_LIMIT = 120
_DEFAULT_RATE_WINDOW = 60.0
_DEFAULT_CURSOR_TTL = 3600
_MAX_TOKENS = 8
_MAX_TOKEN_LENGTH = 512
_MAX_ORIGINS = 20
_MAX_CLIENTS = 10_000
_CURSOR_VERSION = 1
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_B64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class ApiError(Exception):
    """HTTP error with the section 6 body fields."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ApiSettings:
    """Process settings. Secrets stay in the environment that built this object."""

    read_tokens: tuple[str, ...]
    cursor_key: str
    anon_limit: int
    rate_limit: int
    rate_window_seconds: float
    cursor_ttl_seconds: int
    cors_origins: tuple[str, ...]
    database: Path
    artifacts_dir: Path

    @classmethod
    def from_env(cls) -> ApiSettings:
        return cls(
            read_tokens=_tokens(os.environ.get(ENV_READ_TOKENS)),
            cursor_key=os.environ.get(ENV_CURSOR_KEY, ""),
            anon_limit=_positive_int(os.environ.get(ENV_ANON_LIMIT), _DEFAULT_ANON_LIMIT, 10_000),
            rate_limit=_positive_int(os.environ.get(ENV_RATE_LIMIT), _DEFAULT_RATE_LIMIT, 100_000),
            rate_window_seconds=_positive_float(
                os.environ.get(ENV_RATE_WINDOW), _DEFAULT_RATE_WINDOW, 3600.0
            ),
            cursor_ttl_seconds=_bounded_int(
                os.environ.get(ENV_CURSOR_TTL), _DEFAULT_CURSOR_TTL, 60, 86_400
            ),
            cors_origins=_origins(os.environ.get(ENV_CORS)),
            database=_path(os.environ.get(ENV_DATABASE), "artifacts/api/catalog.sqlite"),
            artifacts_dir=_path(os.environ.get(ENV_ARTIFACTS), "artifacts"),
        )


class RateLimiter:
    """In-process sliding window. Each process keeps its own counters."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._drop_stale_keys(now)
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= _MAX_CLIENTS:
                    return False
                events = deque()
                self._events[key] = events
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True

    def _drop_stale_keys(self, now: float) -> None:
        if len(self._events) < _MAX_CLIENTS:
            return
        cutoff = now - self.window_seconds
        stale = [key for key, events in self._events.items() if not events or events[-1] <= cutoff]
        for key in stale:
            del self._events[key]


def accepted_request_id(header: str | None) -> str:
    if header is not None and _REQUEST_ID.fullmatch(header):
        return header
    return uuid4().hex


def request_id_of(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    if isinstance(value, str) and _REQUEST_ID.fullmatch(value):
        return value
    return accepted_request_id(None)


def settings_of(request: Request) -> ApiSettings:
    found = request.app.state.settings
    if not isinstance(found, ApiSettings):
        raise ApiError(503, "unavailable", "api settings are not configured")
    return found


def store_of(request: Request) -> ObjectStore:
    found: object = request.app.state.store
    get = getattr(found, "get", None)
    put = getattr(found, "put", None)
    if not callable(get) or not callable(put):
        raise ApiError(503, "unavailable", "object store is not configured")
    return cast(ObjectStore, found)


def apply_rate_limit(request: Request) -> None:
    limiter = request.app.state.limiter
    if not isinstance(limiter, RateLimiter):
        raise ApiError(503, "unavailable", "rate limit is not configured")
    if not limiter.allow(client_key(request)):
        raise ApiError(429, "rate_limited", "rate limit exceeded")


def require_reader(request: Request) -> None:
    """Allow a configured read token, or a small number of anonymous reads."""
    settings = settings_of(request)
    presented = bearer_token(request.headers.get("authorization"))
    if presented is None:
        gate = request.app.state.anonymous
        if not isinstance(gate, RateLimiter):
            raise ApiError(503, "unavailable", "anonymous access is not configured")
        host = request.client.host if request.client is not None else "unknown"
        if not gate.allow("anon:" + host):
            raise ApiError(401, "unauthorized", "read token required")
        return
    if not presented or not token_accepted(presented, settings.read_tokens):
        raise ApiError(401, "unauthorized", "read token required")


def require_internal(request: Request) -> None:
    host = request.client.host if request.client is not None else None
    if not is_internal_host(host):
        raise ApiError(403, "forbidden", "metrics are restricted to internal clients")


def is_internal_host(host: str | None) -> bool:
    """Loopback and private addresses only. Forwarded headers are not read."""
    if host is None or host == "":
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def bearer_token(header: str | None) -> str | None:
    """Return the token, ``''`` when the header is present but unusable, or None."""
    if header is None:
        return None
    if len(header) > 600:
        return ""
    scheme, separator, rest = header.partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        return ""
    token = rest.strip()
    if not token or len(token) > _MAX_TOKEN_LENGTH or any(char.isspace() for char in token):
        return ""
    return token


def token_accepted(presented: str, allowed: Sequence[str]) -> bool:
    presented_bytes = presented.encode("utf-8")
    matched = False
    for item in allowed:
        try:
            same = hmac.compare_digest(presented_bytes, item.encode("utf-8"))
        except (TypeError, ValueError):
            same = False
        matched = matched or same
    return matched


def client_key(request: Request) -> str:
    header = request.headers.get("authorization")
    if header:
        digest = hashlib.sha256(header.encode("utf-8", errors="replace")).hexdigest()
        return "auth:" + digest
    host = request.client.host if request.client is not None else "unknown"
    return "anon:" + hashlib.sha256(host.encode("utf-8", errors="replace")).hexdigest()


def sign_cursor(
    *,
    key: str,
    kind: str,
    query: Mapping[str, str],
    after_sort: str,
    after_id: str,
    now: float,
    ttl: int,
) -> str:
    if not key:
        raise ApiError(503, "unavailable", "cursor key is not configured")
    payload = {
        "after_id": after_id,
        "after_sort": after_sort,
        "exp": int(now) + ttl,
        "kind": kind,
        "query": {name: query[name] for name in sorted(query)},
        "v": _CURSOR_VERSION,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    mac = hmac.new(key.encode("utf-8"), body, hashlib.sha256).digest()
    return _b64encode(body) + "." + _b64encode(mac)


def verify_cursor(
    token: str,
    *,
    key: str,
    kind: str,
    query: Mapping[str, str],
    now: float,
) -> tuple[str, str]:
    if not key:
        raise ApiError(503, "unavailable", "cursor key is not configured")
    if len(token) > 2048 or token.count(".") != 1:
        raise ApiError(400, "invalid_cursor", "cursor is invalid")
    encoded_body, encoded_mac = token.split(".", 1)
    try:
        body = _b64decode(encoded_body)
        mac = _b64decode(encoded_mac)
    except ValueError:
        raise ApiError(400, "invalid_cursor", "cursor is invalid") from None
    expected = hmac.new(key.encode("utf-8"), body, hashlib.sha256).digest()
    if len(mac) != len(expected) or not hmac.compare_digest(mac, expected):
        raise ApiError(400, "invalid_cursor", "cursor is invalid")
    try:
        payload: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(400, "invalid_cursor", "cursor is invalid") from None
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_cursor", "cursor is invalid")
    if payload.get("v") != _CURSOR_VERSION or payload.get("kind") != kind:
        raise ApiError(400, "invalid_cursor", "cursor is invalid")
    expected_query = {name: query[name] for name in sorted(query)}
    if payload.get("query") != expected_query:
        raise ApiError(400, "invalid_cursor", "cursor is invalid")
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int) or exp <= now:
        raise ApiError(400, "invalid_cursor", "cursor is invalid")
    after_sort = payload.get("after_sort")
    after_id = payload.get("after_id")
    if (
        not isinstance(after_sort, str)
        or not isinstance(after_id, str)
        or not after_sort
        or not after_id
        or len(after_sort) > 64
        or len(after_id) > 256
    ):
        raise ApiError(400, "invalid_cursor", "cursor is invalid")
    return after_sort, after_id


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    if not data or any(char not in _B64_ALPHABET for char in data):
        raise ValueError("cursor encoding is invalid")
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding)
    except ValueError as exc:
        raise ValueError("cursor encoding is invalid") from exc


def _tokens(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    found: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token or len(token) > _MAX_TOKEN_LENGTH or any(char.isspace() for char in token):
            continue
        if token not in found:
            found.append(token)
        if len(found) == _MAX_TOKENS:
            break
    return tuple(found)


def _origins(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    found: list[str] = []
    for part in raw.split(","):
        origin = part.strip()
        # A wildcard is not an allow-list entry.
        if not origin or origin == "*" or len(origin) > 200 or origin in found:
            continue
        found.append(origin)
        if len(found) == _MAX_ORIGINS:
            break
    return tuple(found)


def _path(raw: str | None, default: str) -> Path:
    if raw is None or not raw.strip():
        return Path(default)
    return Path(raw)


def _positive_int(raw: str | None, default: int, upper: int) -> int:
    return _bounded_int(raw, default, 1, upper)


def _bounded_int(raw: str | None, default: int, lower: int, upper: int) -> int:
    if raw is None or not raw.isdigit():
        return default
    value = int(raw)
    if value < lower or value > upper:
        return default
    return value


def _positive_float(raw: str | None, default: float, upper: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value <= 0 or value > upper:
        return default
    return value
