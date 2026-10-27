"""S20 egress, tool grants, untrusted README, and log redaction."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from pytest import MonkeyPatch

from techpulse.contracts import RawRecord
from techpulse.governance.egress import (
    AsyncGuardedTransport,
    GuardedTransport,
    check_egress,
    egress_request_hook,
    load_domain_allowlist,
)
from techpulse.governance.policy import (
    HttpGetArgs,
    PolicyBlocked,
    authorize_tool_call,
    executable_requests,
    ingest_untrusted,
    load_policy,
    perform_tool_call,
    unauthorized_tool_call_count,
    write_audit_log,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "config" / "policy.yaml"
REGISTRY = ROOT / "tests" / "fixtures" / "source_registry.yaml"
README = ROOT / "tests" / "fixtures" / "malicious_readme.md"

ROLES = {
    "GrokbotOrchestrator",
    "InfraAgent",
    "PolicyAgent",
    "CollectorAgent",
    "CleanerAgent",
    "IndexAgent",
    "SignalAgent",
    "EvidenceAgent",
    "WriterAgent",
    "QAAgent",
    "APIAgent",
    "DeployAgent",
    "OpsAgent",
    "GrowthAgent",
}

RAW: dict[str, object] = {
    "schema_version": "1.0",
    "source_id": "openalex",
    "source_record_id": "W0000000000",
    "canonical_url": "https://example.org/example",
    "title": "Example title",
    "abstract_or_excerpt": "Example excerpt",
    "published_at": "2026-10-26T10:00:00Z",
    "collected_at": "2026-10-27T00:00:00Z",
    "language": "en",
    "license_status": "metadata_only",
    "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "policy_snapshot_id": "openalex-2026-10-27",
}


class RecordingTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, request=request)


class RecordingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, request=request)


def _check(role: str, method: str, url: str) -> None:
    check_egress(role, method, url, registry_path=REGISTRY, policy_path=POLICY)


def _policy_text(domain: str) -> str:
    return (
        "default: deny\n"
        "network_allowlist:\n"
        "  domains:\n"
        f"    - {domain}\n"
        "roles:\n"
        "  CollectorAgent:\n"
        "    network: allowlisted_get\n"
        "    tools:\n"
        "      - http_get\n"
        "      - put_raw\n"
    )


def test_policy_matrix_is_default_deny() -> None:
    policy = load_policy(POLICY)
    assert set(policy.roles) == ROLES
    networked = {name for name, grant in policy.roles.items() if grant.network != "none"}
    assert networked == {"CollectorAgent"}
    assert policy.grant("CollectorAgent").network == "allowlisted_get"
    assert policy.grant("NoSuchAgent").network == "none"
    assert policy.grant("NoSuchAgent").tools == frozenset()
    assert "api.openalex.org" in policy.fallback_domains
    for grant in policy.roles.values():
        assert grant.tools
        assert "publish_report" not in grant.tools
        assert "shell" not in grant.tools


def test_unknown_domain_rejected() -> None:
    inner = RecordingTransport()
    hook = egress_request_hook("CollectorAgent", registry_path=REGISTRY, policy_path=POLICY)
    with httpx.Client(transport=inner, event_hooks={"request": [hook]}) as client:
        with pytest.raises(PolicyBlocked):
            client.get("https://unknown.example/x")
        with pytest.raises(PolicyBlocked):
            client.get("https://api.openalex.org.evil.example/works")
        with pytest.raises(PolicyBlocked):
            client.get("https://evil.example/api.openalex.org")
    assert inner.calls == 0


def test_post_rejected() -> None:
    inner = RecordingTransport()
    transport = GuardedTransport(
        "CollectorAgent",
        inner,
        registry_path=REGISTRY,
        policy_path=POLICY,
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(PolicyBlocked):
            client.post("https://api.openalex.org/works")
    assert inner.calls == 0


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_non_read_methods_rejected(method: str) -> None:
    with pytest.raises(PolicyBlocked):
        _check("CollectorAgent", method, "https://api.openalex.org/works")


def test_cleaner_has_no_network() -> None:
    inner = RecordingTransport()
    transport = GuardedTransport(
        "Cleaner",
        inner,
        registry_path=REGISTRY,
        policy_path=POLICY,
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(PolicyBlocked):
            client.get("https://api.openalex.org/works")
    assert inner.calls == 0
    with pytest.raises(PolicyBlocked):
        _check("CleanerAgent", "GET", "https://api.openalex.org/works")


@pytest.mark.parametrize("role", ["WriterAgent", "QAAgent"])
def test_writer_and_qa_have_no_network(role: str) -> None:
    with pytest.raises(PolicyBlocked):
        _check(role, "GET", "https://api.openalex.org/works")
    with pytest.raises(PolicyBlocked):
        _check(role, "HEAD", "https://api.openalex.org/works")


def test_collector_get_and_head_to_allowlisted_domain() -> None:
    inner = RecordingTransport()
    transport = GuardedTransport(
        "CollectorAgent",
        inner,
        registry_path=REGISTRY,
        policy_path=POLICY,
    )
    with httpx.Client(transport=transport) as client:
        get_response = client.get("https://API.OpenAlex.org/works")
        head_response = client.head("https://api.openalex.org/works")
    assert get_response.status_code == 200
    assert head_response.status_code == 200
    assert inner.calls == 2
    get_response.close()
    head_response.close()


def test_present_registry_replaces_policy_allowlist() -> None:
    policy = load_policy(POLICY)
    registry_hosts = load_domain_allowlist(registry_path=REGISTRY, policy_path=POLICY)
    fallback_only = policy.fallback_domains - registry_hosts
    assert fallback_only
    host = sorted(fallback_only)[0]
    with pytest.raises(PolicyBlocked):
        _check("CollectorAgent", "GET", f"https://{host}/")
    _check("CollectorAgent", "GET", "https://api.openalex.org/works")
    with pytest.raises(PolicyBlocked):
        _check("CollectorAgent", "GET", "https://disabled.example/")


def test_allowlist_falls_back_to_policy_when_registry_missing() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        policy_path = root / "policy.yaml"
        policy_path.write_text(_policy_text("fallback.example"), encoding="utf-8")
        missing = root / "source_registry.yaml"
        check_egress(
            "CollectorAgent",
            "GET",
            "https://fallback.example/x",
            registry_path=missing,
            policy_path=policy_path,
        )
        with pytest.raises(PolicyBlocked):
            check_egress(
                "CollectorAgent",
                "GET",
                "https://other.example/x",
                registry_path=missing,
                policy_path=policy_path,
            )


def test_registry_reads_whitelist_and_base_url() -> None:
    document = (
        "sources:\n"
        "  openalex:\n"
        "    enabled: true\n"
        "    domain_whitelist:\n"
        "      - api.openalex.org\n"
        "  crossref:\n"
        "    enabled: true\n"
        "    base_url: https://api.crossref.org\n"
        "  hidden:\n"
        "    enabled: false\n"
        "    domains:\n"
        "      - hidden.example\n"
    )
    with TemporaryDirectory() as directory:
        root = Path(directory)
        registry = root / "source_registry.yaml"
        policy_path = root / "policy.yaml"
        registry.write_text(document, encoding="utf-8")
        policy_path.write_text(_policy_text("other.example"), encoding="utf-8")
        check_egress(
            "CollectorAgent",
            "GET",
            "https://api.openalex.org/x",
            registry_path=registry,
            policy_path=policy_path,
        )
        check_egress(
            "CollectorAgent",
            "HEAD",
            "https://api.crossref.org/x",
            registry_path=registry,
            policy_path=policy_path,
        )
        with pytest.raises(PolicyBlocked):
            check_egress(
                "CollectorAgent",
                "GET",
                "https://hidden.example/x",
                registry_path=registry,
                policy_path=policy_path,
            )
        with pytest.raises(PolicyBlocked):
            check_egress(
                "CollectorAgent",
                "GET",
                "https://other.example/x",
                registry_path=registry,
                policy_path=policy_path,
            )


def test_broken_registry_does_not_use_policy_allowlist() -> None:
    with TemporaryDirectory() as directory:
        registry = Path(directory) / "source_registry.yaml"
        registry.write_text("sources: [\n", encoding="utf-8")
        with pytest.raises(PolicyBlocked):
            check_egress(
                "CollectorAgent",
                "GET",
                "https://api.github.com/",
                registry_path=registry,
                policy_path=POLICY,
            )


def test_credential_urls_are_rejected() -> None:
    url = "https://" + "user" + ":" + "pw" + "@api.openalex.org/works"
    with pytest.raises(PolicyBlocked):
        _check("CollectorAgent", "GET", url)


def test_malicious_readme_does_not_trigger_tool_or_command(monkeypatch: MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("command was executed")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(os, "system", fail)
    raw = README.read_text(encoding="utf-8")
    raw_commands, raw_tools = executable_requests(raw)
    assert raw_commands
    assert raw_tools
    result = ingest_untrusted(raw)
    commands, tools = executable_requests(result.data.marked())
    assert commands == []
    assert tools == []
    assert result.commands_executed == 0
    assert result.tool_calls_executed == 0
    assert "evil.example" in result.data.marked()
    assert "</untrusted_data>" not in result.data.marked().split("\n", 1)[1].rsplit("\n", 1)[0]
    assert unauthorized_tool_call_count() == 0


def test_unauthorized_tool_call_count_is_zero() -> None:
    with pytest.raises(PolicyBlocked):
        perform_tool_call("CleanerAgent", "put_raw", RAW, policy_path=POLICY)
    with pytest.raises(PolicyBlocked):
        perform_tool_call("WriterAgent", "publish_report", {}, policy_path=POLICY)
    with pytest.raises(PolicyBlocked):
        authorize_tool_call(
            "QAAgent",
            "write_repo",
            {"record_id": "x", "body": "y"},
            policy_path=POLICY,
        )
    parsed = perform_tool_call("CollectorAgent", "put_raw", RAW, policy_path=POLICY)
    assert isinstance(parsed, RawRecord)
    assert unauthorized_tool_call_count() == 0


def test_put_raw_schema_validation() -> None:
    with pytest.raises(PolicyBlocked):
        authorize_tool_call("CollectorAgent", "put_raw", {}, policy_path=POLICY)
    parsed = authorize_tool_call("CollectorAgent", "put_raw", RAW, policy_path=POLICY)
    assert isinstance(parsed, RawRecord)


def test_http_get_tool_uses_egress_allowlist() -> None:
    parsed = authorize_tool_call(
        "CollectorAgent",
        "http_get",
        {"url": "https://api.openalex.org/works?page=1"},
        registry_path=REGISTRY,
        policy_path=POLICY,
    )
    assert isinstance(parsed, HttpGetArgs)
    assert parsed.url == "https://api.openalex.org/works?page=1"
    with pytest.raises(PolicyBlocked):
        authorize_tool_call(
            "CollectorAgent",
            "http_get",
            {"url": "https://unknown.example/x"},
            registry_path=REGISTRY,
            policy_path=POLICY,
        )


def test_secret_looking_string_is_redacted_in_logs() -> None:
    marker = "ghp_" + ("b" * 36)
    with TemporaryDirectory() as directory:
        path = Path(directory) / "audit.log"
        write_audit_log(path, f"upstream body contained {marker}")
        text = path.read_text(encoding="utf-8")
    assert marker not in text
    assert "[REDACTED]" in text


@pytest.mark.asyncio
async def test_async_transport_blocks_post() -> None:
    inner = RecordingAsyncTransport()
    transport = AsyncGuardedTransport(
        "CollectorAgent",
        inner,
        registry_path=REGISTRY,
        policy_path=POLICY,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PolicyBlocked):
            await client.post("https://api.openalex.org/works")
    assert inner.calls == 0
