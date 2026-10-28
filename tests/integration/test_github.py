"""C20 GitHub fixture tests; no live HTTP requests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from pytest import MonkeyPatch

from techpulse.collectors.github import CollectorError, GitHubCollector
from techpulse.contracts import TaskStatus
from techpulse.governance.policy import PolicyBlocked

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "github"
REGISTRY = ROOT / "config" / "source_registry.yaml"
POLICY = ROOT / "config" / "policy.yaml"


class FixtureTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.secondary_limited = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.secondary_limited:
            return httpx.Response(
                403,
                headers={"Retry-After": "120"},
                json={"message": "You have exceeded a secondary rate limit."},
                request=request,
            )
        if request.url.path.endswith("/releases"):
            payload = json.loads((FIXTURES / "releases.json").read_text(encoding="utf-8"))
        else:
            payload = json.loads((FIXTURES / "repository.json").read_text(encoding="utf-8"))
            if "old-owner" in request.url.path:
                payload["full_name"] = "old-owner/signal-kit"
                payload["owner"]["login"] = "old-owner"
                payload["html_url"] = "https://github.com/old-owner/signal-kit"
        return httpx.Response(200, json=payload, request=request)


def _collector(transport: FixtureTransport) -> GitHubCollector:
    return GitHubCollector(
        registry_path=REGISTRY,
        policy_path=POLICY,
        transport=transport,
        sleeper=lambda _seconds: None,
    )


def test_repository_id_is_stable_across_rename_and_cursor_is_idempotent() -> None:
    transport = FixtureTransport()
    with _collector(transport) as collector:
        before = collector.collect(["old-owner/signal-kit"])
        after = collector.collect(["new-owner/signal-kit"], before.cursor)
        repeated = collector.collect(["old-owner/signal-kit"], after.cursor)
    assert before.records[0].source_record_id == "83294721"
    assert after.records == ()
    assert after.cursor == before.cursor
    assert repeated.records == ()


def test_only_get_requests_and_token_never_logged(monkeypatch: MonkeyPatch) -> None:
    token = "fixture-token-never-log"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    messages: list[str] = []
    monkeypatch.setattr("builtins.print", lambda message="": messages.append(str(message)))
    transport = FixtureTransport()
    with _collector(transport) as collector:
        collector.collect(["new-owner/signal-kit"])
    assert all(request.method == "GET" for request in transport.requests)
    assert transport.requests[0].headers["Authorization"] == f"Bearer {token}"
    assert token not in "\n".join(messages)


def test_secondary_rate_limit_opens_breaker_and_cursor_does_not_advance() -> None:
    transport = FixtureTransport()
    transport.secondary_limited = True
    with _collector(transport) as collector:
        with pytest.raises(CollectorError) as failure:
            collector.collect(["new-owner/signal-kit"], '["83290000"]')
        assert failure.value.status is TaskStatus.RETRYABLE_ERROR
        assert failure.value.retry_after == 120
        assert collector.circuit_open
        calls = len(transport.requests)
        with pytest.raises(CollectorError):
            collector.collect(["new-owner/signal-kit"])
    assert len(transport.requests) == calls


def test_primary_rate_limit_is_retryable() -> None:
    class LimitedTransport(FixtureTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4102444800"},
                request=request,
            )

    with _collector(LimitedTransport()) as collector:
        with pytest.raises(CollectorError) as failure:
            collector.collect(["new-owner/signal-kit"])
    assert failure.value.status is TaskStatus.RETRYABLE_ERROR


def test_malicious_repository_text_is_never_executed(monkeypatch: MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("repository content executed")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(os, "system", fail)
    with _collector(FixtureTransport()) as collector:
        result = collector.collect(["new-owner/signal-kit"])
    serialized = result.records[0].model_dump_json()
    assert "evil.example" not in serialized
    assert "powershell" not in serialized
    assert "do-not-store@example.test" not in serialized


def test_egress_denies_writes() -> None:
    transport = FixtureTransport()
    with _collector(transport) as collector:
        with pytest.raises(PolicyBlocked):
            collector._client.post("https://api.github.com/repos/new-owner/signal-kit")
    assert transport.requests == []
