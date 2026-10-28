"""GitHub read-only collector."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from techpulse.contracts import LicenseStatus, RawRecord, SourceId, TaskStatus
from techpulse.governance.egress import GuardedTransport
from techpulse.governance.policy import PolicyBlocked

_ROLE = "CollectorAgent"
_API_ROOT = "https://api.github.com"
_USER_AGENT = "TechPulse-AI/1.0 (https://github.com/techpulse; contact: info@techpulse.example.com)"


class CollectorError(RuntimeError):
    """A source failure with the task status required by the execution plan."""

    def __init__(self, message: str, status: TaskStatus, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class GitHubCollection:
    records: tuple[RawRecord, ...]
    cursor: str
    status: TaskStatus = TaskStatus.SUCCESS


@dataclass
class _GitHubPolicy:
    enabled: bool
    policy_snapshot_id: str
    requests_per_minute: float


@dataclass
class GitHubCollector:
    """Collect public repository metadata without executing repository content."""

    registry_path: Path | None = None
    policy_path: Path | None = None
    transport: httpx.BaseTransport | None = None
    sleeper: Callable[[float], None] = time.sleep
    timeout: float = 10.0
    circuit_open: bool = False
    _last_request_at: float | None = field(default=None, init=False)
    _client: httpx.Client = field(init=False)
    _policy: _GitHubPolicy = field(init=False)

    def __post_init__(self) -> None:
        self._policy = self._load_policy()
        guarded = GuardedTransport(
            _ROLE,
            self.transport,
            registry_path=self.registry_path,
            policy_path=self.policy_path,
        )
        self._client = httpx.Client(
            transport=guarded,
            headers={"User-Agent": _USER_AGENT},
            timeout=self.timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubCollector:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def collect(self, repositories: list[str], cursor: str | None = None) -> GitHubCollection:
        """Return repositories after the cursor; failures leave the cursor unchanged."""
        seen = set(_decode_cursor(cursor))
        records: list[RawRecord] = []
        visited: set[str] = set()
        for name in repositories:
            payload = self._get_json(f"/repos/{_repository_path(name)}")
            repository = _mapping(payload, "repository")
            repository_id = str(_required(repository, "id", int))
            if repository_id in seen or repository_id in visited:
                continue
            records.append(self._repository_record(repository))
            visited.add(repository_id)
        return GitHubCollection(tuple(records), _encode_cursor(seen | visited))

    def _repository_record(self, repository: dict[str, Any]) -> RawRecord:
        repository_id = str(_required(repository, "id", int))
        full_name = str(_required(repository, "full_name", str))
        name = str(_required(repository, "name", str))
        html_url = str(_required(repository, "html_url", str))
        owner = _mapping(repository.get("owner"), "owner")
        owner_login = str(_required(owner, "login", str))
        # Releases contain no author/profile fields and no repository instructions.
        release_path = f"/repos/{quote(owner_login, safe='')}/{quote(name, safe='')}/releases"
        releases_payload = self._get_json(f"{release_path}?per_page=1")
        releases = _list(releases_payload, "releases")
        release = _mapping(releases[0], "release") if releases else {}
        license_info = repository.get("license")
        license_name = (
            str(license_info.get("spdx_id", "")) if isinstance(license_info, dict) else ""
        )
        license_status = (
            LicenseStatus.METADATA_ONLY
            if not license_name or license_name == "NOASSERTION"
            else LicenseStatus.REUSABLE_EXCERPT
        )
        release_name = release.get("name") or release.get("tag_name") or "No releases"
        excerpt = (
            f"Repository {full_name}; stars: {repository.get('stargazers_count', 0)}; "
            f"forks: {repository.get('forks_count', 0)}; "
            f"open issues: {repository.get('open_issues_count', 0)}; "
            f"archived: {str(repository.get('archived', False)).lower()}; "
            f"latest release: {release_name}. Metadata only; README not retrieved or executed."
        )
        published_at = release.get("published_at") or repository.get("pushed_at")
        raw = _raw_record(
            schema_version="1.0",
            source_id=SourceId.GITHUB,
            source_record_id=repository_id,
            canonical_url=html_url,
            title=full_name,
            abstract_or_excerpt=excerpt,
            published_at=published_at,
            published_at_missing_reason=(
                None if published_at else "repository has no update timestamp"
            ),
            collected_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            language=str(repository.get("language") or "und"),
            license_status=license_status,
            content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
            policy_snapshot_id=self._policy.policy_snapshot_id,
        )
        return raw

    def _get_json(self, path: str) -> Any:
        if not self._policy.enabled:
            raise CollectorError("GitHub source is disabled", TaskStatus.POLICY_BLOCKED)
        if self.circuit_open:
            raise CollectorError(
                "GitHub secondary rate-limit breaker is open", TaskStatus.RETRYABLE_ERROR
            )
        self._throttle()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._client.get(f"{_API_ROOT}{path}", headers=headers, timeout=self.timeout)
        except PolicyBlocked:
            raise
        except httpx.HTTPError as exc:
            raise CollectorError("GitHub network failure", TaskStatus.RETRYABLE_ERROR) from exc
        self._check_response(response)
        return response.json()

    def _check_response(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = response.text.lower()
        secondary = response.status_code in {403, 429} and (
            "secondary rate limit" in message or "retry-after" in response.headers
        )
        primary = (
            response.status_code in {403, 429}
            and response.headers.get("x-ratelimit-remaining") == "0"
        )
        if secondary:
            self.circuit_open = True
            raise CollectorError(
                "GitHub secondary rate limit",
                TaskStatus.RETRYABLE_ERROR,
                _retry_delay(response),
            )
        if response.status_code == 429 or primary:
            raise CollectorError(
                "GitHub primary rate limit", TaskStatus.RETRYABLE_ERROR, _retry_delay(response)
            )
        if response.status_code in {401, 403} or "captcha" in message:
            raise CollectorError("GitHub request blocked by policy", TaskStatus.POLICY_BLOCKED)
        if response.status_code >= 500:
            raise CollectorError("GitHub server failure", TaskStatus.RETRYABLE_ERROR)
        raise CollectorError("GitHub request failed", TaskStatus.FATAL_ERROR)

    def _throttle(self) -> None:
        now = time.monotonic()
        interval = 60.0 / self._policy.requests_per_minute
        if self._last_request_at is not None:
            self.sleeper(max(0.0, interval - (now - self._last_request_at)))
        self._last_request_at = time.monotonic()

    def _load_policy(self) -> _GitHubPolicy:
        import yaml

        default_path = Path(__file__).resolve().parents[3] / "config" / "source_registry.yaml"
        path = self.registry_path or default_path
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        source = document["sources"]["github"]
        return _GitHubPolicy(
            enabled=source["enabled"] is True,
            policy_snapshot_id=str(source["policy_snapshot_id"]),
            requests_per_minute=float(source["requests_per_minute"]),
        )


def _raw_record(**values: Any) -> RawRecord:
    return RawRecord(**values)


def _retry_delay(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    reset = response.headers.get("x-ratelimit-reset")
    return max(0.0, float(reset) - time.time()) if reset else None


def _repository_path(name: str) -> str:
    parts = name.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repository must be owner/name")
    return "/".join(quote(part, safe="") for part in parts)


def _encode_cursor(ids: set[str]) -> str:
    return json.dumps(sorted(ids), separators=(",", ":"))


def _decode_cursor(cursor: str | None) -> list[str]:
    if not cursor:
        return []
    value = json.loads(cursor)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("invalid GitHub cursor")
    return value


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CollectorError(f"Invalid GitHub {label}", TaskStatus.FATAL_ERROR)
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CollectorError(f"Invalid GitHub {label}", TaskStatus.FATAL_ERROR)
    return value


def _required(mapping: dict[str, Any], key: str, kind: type) -> Any:
    value = mapping.get(key)
    if not isinstance(value, kind) or isinstance(value, bool):
        raise CollectorError(f"Invalid GitHub {key}", TaskStatus.FATAL_ERROR)
    return value
