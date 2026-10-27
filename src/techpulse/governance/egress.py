"""Outbound HTTP allow-list. Every client call goes through check_egress."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from techpulse.governance.policy import PolicyBlocked, load_policy, normalize_domain

# S10 may emit a list or a mapping. Enabled entries contribute domains,
# domain_whitelist, allowed_domains, whitelist, and the host of base_url.
_DOMAIN_KEYS = ("domains", "domain_whitelist", "allowed_domains", "whitelist")


def load_domain_allowlist(
    *,
    registry_path: Path | None = None,
    policy_path: Path | None = None,
) -> frozenset[str]:
    """Enabled source domains when the registry file exists, otherwise policy.yaml."""
    registry = (
        registry_path
        if registry_path is not None
        else _project_root() / "config" / "source_registry.yaml"
    )
    if registry.is_file():
        return _domains_from_registry_file(registry)
    domains = load_policy(policy_path).fallback_domains
    return frozenset(normalize_domain(item) for item in domains)


def check_egress(
    role: str,
    method: str,
    url: str,
    *,
    registry_path: Path | None = None,
    policy_path: Path | None = None,
) -> None:
    """Allow GET/HEAD to an allowlisted host for roles with allowlisted_get. Deny the rest."""
    grant = load_policy(policy_path).grant(role)
    if grant.network != "allowlisted_get":
        raise PolicyBlocked(f"{role} has no network access")
    method_name = method.upper()
    if method_name not in {"GET", "HEAD"}:
        raise PolicyBlocked(f"{method_name} is not allowed")
    host = _request_host(url)
    allowlist = load_domain_allowlist(registry_path=registry_path, policy_path=policy_path)
    if host not in allowlist:
        raise PolicyBlocked(f"{method_name} {host} is not allowlisted")


def egress_request_hook(
    role: str,
    *,
    registry_path: Path | None = None,
    policy_path: Path | None = None,
) -> Callable[[httpx.Request], None]:
    def hook(request: httpx.Request) -> None:
        check_egress(
            role,
            request.method,
            str(request.url),
            registry_path=registry_path,
            policy_path=policy_path,
        )

    return hook


class GuardedTransport(httpx.BaseTransport):
    """Sync httpx transport that checks egress before the inner transport runs."""

    def __init__(
        self,
        role: str,
        transport: httpx.BaseTransport | None = None,
        *,
        registry_path: Path | None = None,
        policy_path: Path | None = None,
    ) -> None:
        self._role = role
        self._transport = httpx.HTTPTransport() if transport is None else transport
        self._registry_path = registry_path
        self._policy_path = policy_path

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        check_egress(
            self._role,
            request.method,
            str(request.url),
            registry_path=self._registry_path,
            policy_path=self._policy_path,
        )
        return self._transport.handle_request(request)

    def close(self) -> None:
        self._transport.close()


class AsyncGuardedTransport(httpx.AsyncBaseTransport):
    """Async httpx transport that checks egress before the inner transport runs."""

    def __init__(
        self,
        role: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        registry_path: Path | None = None,
        policy_path: Path | None = None,
    ) -> None:
        self._role = role
        self._transport = httpx.AsyncHTTPTransport() if transport is None else transport
        self._registry_path = registry_path
        self._policy_path = policy_path

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        check_egress(
            self._role,
            request.method,
            str(request.url),
            registry_path=self._registry_path,
            policy_path=self._policy_path,
        )
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _request_host(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise PolicyBlocked("only http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise PolicyBlocked("URLs must not contain credentials")
    if parsed.hostname is None:
        raise PolicyBlocked("URL has no host")
    try:
        return normalize_domain(parsed.hostname)
    except ValueError as exc:
        raise PolicyBlocked("URL has no host") from exc


def _domains_from_registry_file(path: Path) -> frozenset[str]:
    try:
        document: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        hosts = _domains_from_document(document)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise PolicyBlocked("source registry cannot be read") from exc
    return frozenset(hosts)


def _domains_from_document(document: object) -> set[str]:
    if document is None:
        return set()
    found: set[str] = set()
    for source in _source_items(document):
        enabled = source.get("enabled")
        if enabled is not True:
            continue
        found.update(_source_domains(source))
    return found


def _source_items(document: object) -> list[dict[str, object]]:
    if isinstance(document, list):
        return [_as_mapping(item) for item in document]
    mapping = _as_mapping(document)
    raw_sources = mapping.get("sources", mapping.get("source_registry"))
    if isinstance(raw_sources, list):
        return [_as_mapping(item) for item in raw_sources]
    if isinstance(raw_sources, dict):
        items: list[dict[str, object]] = []
        for key, value in raw_sources.items():
            if not isinstance(key, str):
                raise ValueError("source id must be a string")
            item = _as_mapping(value)
            item.setdefault("source_id", key)
            items.append(item)
        return items
    raise ValueError("source registry has no sources")


def _source_domains(source: dict[str, object]) -> set[str]:
    found: set[str] = set()
    for key in _DOMAIN_KEYS:
        if key not in source:
            continue
        for item in _as_str_list(source[key]):
            found.add(normalize_domain(item))
    base_url = source.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        found.add(normalize_domain(base_url))
    if not found:
        raise ValueError("enabled source has no domains")
    return found


def _as_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a mapping")
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("expected string keys")
        mapped[key] = item
    return mapped


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("expected a list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("expected domain strings")
        items.append(item.strip())
    return items
