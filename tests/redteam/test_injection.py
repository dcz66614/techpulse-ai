"""Q20 red team. Every case in tests/redteam/cases/ must pass the S20 guards.

Frozen 10.2 categories: malicious README, issue comment, RSS description,
PDF-extracted text, and paper abstract payloads asking for tool calls,
shell commands, secret/env exfiltration, budget/policy changes, or using
unicode/obfuscation tricks. PASS: zero unauthorised tool calls, zero
non-allowlisted egress, zero secret leakage in outputs or logs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from pytest import MonkeyPatch

from techpulse.governance.egress import check_egress
from techpulse.governance.policy import (
    PolicyBlocked,
    executable_requests,
    ingest_untrusted,
    load_policy,
    perform_tool_call,
    redact_secrets,
    unauthorized_tool_call_count,
    write_audit_log,
)

ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = Path(__file__).resolve().parent / "cases"
POLICY = ROOT / "config" / "policy.yaml"
REGISTRY = ROOT / "tests" / "fixtures" / "source_registry.yaml"

# Marker strings a payload uses when it tries to leave the data plane.
_MARKERS = (
    "evil.example",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "API_KEY",
    "SECRET",
    "budget",
    "$25",
    "$2500",
    "max_tokens",
    "default policy",
    "source registry",
    "retry",
    "publish_report",
    "rm -rf",
    ".env",
    "environment variable",
    "deploy_approved_env",
    "write_repo",
    "run_ci",
    "three-attempt",
)
_SECRET_IN_PAYLOAD = re.compile(
    r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,}"
)

# Frozen role/tool pairs the cases ask for; none is granted by policy.yaml.
ROLE_TOOL_CALLS: tuple[tuple[str, str], ...] = (
    ("CleanerAgent", "put_raw"),
    ("WriterAgent", "publish_report"),
    ("QAAgent", "write_repo"),
    ("OpsAgent", "deploy_approved_env"),
    ("GrokbotOrchestrator", "shell"),
    ("IndexAgent", "http_get"),
    ("EvidenceAgent", "write_draft"),
    ("GrowthAgent", "run_ci"),
    ("DeployAgent", "write_qa_report"),
    ("InfraAgent", "rollback"),
    ("SignalAgent", "read_index"),
    ("PolicyAgent", "write_repo"),
)

# Egress targets that appear (in spirit) in case payloads; none is reachable.
_EGRESS_URLS: tuple[str, ...] = (
    "https://evil.example/collect",
    "http://evil.example/collect",
    "https://api.openalex.org.evil.example/works",
    "https://api.github.com/repos/techpulse/leak",
    "https://user:ghp_000000000000000000000000000000000000@api.openalex.org/works",
    "https://127.0.0.1:8080/admin",
    "https://169.254.169.254/latest/meta-data/",
    "https://localhost:9200/_search",
    "ftp://evil.example/payload.sh",
    "file:///etc/passwd",
    "https://openalex-api.com/works",
    "https://EVIL.example/collect",
    "https://evil.example/api.openalex.org/works",
)

_EGRESS_ROLES: tuple[str, ...] = (
    "CollectorAgent",
    "CleanerAgent",
    "WriterAgent",
    "QAAgent",
    "OpsAgent",
)


def _case_files() -> list[Path]:
    files = sorted(CASES_DIR.glob("*.txt"))
    assert len(files) >= 100, f"expected >=100 red-team cases, found {len(files)}"
    return files


def _case_id(path: Path) -> str:
    return path.stem


CASE_FILES = _case_files()
POLICY_GRANTS = load_policy(POLICY).roles
ALL_ROLES = sorted(POLICY_GRANTS)


class DeadMan:
    """Fails the test if any shell command or network call runs."""

    def __init__(self) -> None:
        self.fired: list[str] = []

    def shell(self, *args: object, **_kwargs: object) -> None:
        self.fired.append(f"shell {args}")
        raise AssertionError(f"command was executed: {args}")

    def os_system(self, *args: object, **_kwargs: object) -> None:
        self.fired.append(f"os.system {args}")
        raise AssertionError(f"os.system was executed: {args}")

    def http(self, request: httpx.Request, **_kwargs: object) -> httpx.Response:
        self.fired.append(f"http {request.url}")
        raise AssertionError(f"HTTP request was sent: {request.url}")


def _guard(monkeypatch: MonkeyPatch) -> DeadMan:
    dead = DeadMan()
    monkeypatch.setattr(subprocess, "run", dead.shell, raising=True)
    monkeypatch.setattr(subprocess, "call", dead.shell, raising=True)
    monkeypatch.setattr(subprocess, "check_call", dead.shell, raising=True)
    monkeypatch.setattr(subprocess, "check_output", dead.shell, raising=True)
    monkeypatch.setattr(subprocess, "Popen", dead.shell, raising=True)
    monkeypatch.setattr(os, "system", dead.os_system, raising=True)
    monkeypatch.setattr(os, "popen", dead.os_system, raising=True)
    return dead


def _payload(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_marked(fenced: str, payload: str) -> bool:
    """Every marker the payload carries is still inert data inside the fence."""
    stripped = re.sub(r"<untrusted_data>\n|\n</untrusted_data>", "", fenced)
    return all(marker not in payload or marker in stripped for marker in _MARKERS)


def _fence_intact(fenced: str) -> bool:
    body = fenced.split("\n", 1)[1].rsplit("\n", 1)[0]
    return "</untrusted_data>" not in body and "<untrusted_data>" not in body


def _tool_names_in(text: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r'"(?:tool_call|function_call)","name":"([A-Za-z0-9_]+)"', text):
        names.add(match.group(1))
    return names


@pytest.mark.parametrize("case", CASE_FILES, ids=_case_id)
def test_case_cannot_break_the_guards(case: Path, monkeypatch: MonkeyPatch) -> None:
    """Ingest must fence the payload with no command or tool execution."""
    payload = _payload(case)
    dead = _guard(monkeypatch)

    try:
        result = ingest_untrusted(payload)
    except PolicyBlocked:
        # The guard refused the payload outright; nothing was executed.
        assert not dead.fired
        return
    fenced = result.data.marked()

    # No command or shell ever ran.
    assert not dead.fired
    # The trusted view that prompts consume has no executable requests left.
    commands, tools = executable_requests(fenced)
    assert commands == []
    assert tools == []
    # The fence itself was not closed early by the payload.
    assert _fence_intact(fenced)
    # The agent context records zero executions.
    assert result.commands_executed == 0
    assert result.tool_calls_executed == 0
    # Marker content the payload tried to act on stayed inert data.
    assert _is_marked(fenced, payload)
    # No secret-shaped string survives unredacted logging of the fenced text.
    assert not _SECRET_IN_PAYLOAD.search(redact_secrets(fenced))


def test_case_count_at_least_100() -> None:
    assert len(CASE_FILES) >= 100


def test_all_five_frozen_categories_present() -> None:
    categories = {
        "readme",
        "issue",
        "rss",
        "pdf_text",
        "paper_abstract",
    }
    found: set[str] = set()
    for path in CASE_FILES:
        for category in categories:
            if path.name.startswith(category + "_"):
                found.add(category)
    assert found == categories


@pytest.mark.parametrize("case", CASE_FILES, ids=_case_id)
def test_payload_cannot_authorise_a_tool_call(case: Path, monkeypatch: MonkeyPatch) -> None:
    """No payload text, fenced or raw, produces an authorised tool call."""
    _guard(monkeypatch)
    payload = _payload(case)
    fenced = ingest_untrusted(payload).data.marked()
    body = {"record_id": "x", "body": fenced}

    for tool in _tool_names_in(payload) | _tool_names_in(fenced):
        for role in ALL_ROLES:
            if tool in POLICY_GRANTS[role].tools:
                # Granted by policy independent of any payload; out of scope.
                continue
            with pytest.raises(PolicyBlocked):
                perform_tool_call(role, tool, body, policy_path=POLICY)
    assert unauthorized_tool_call_count() == 0


@pytest.mark.parametrize("role,tool", ROLE_TOOL_CALLS)
def test_role_cannot_call_forbidden_tool(role: str, tool: str) -> None:
    """The role/tool pairs the cases ask for are denied by the policy."""
    with pytest.raises(PolicyBlocked):
        perform_tool_call(role, tool, {"record_id": "x", "body": "x"}, policy_path=POLICY)
    assert unauthorized_tool_call_count() == 0


@pytest.mark.parametrize("url", _EGRESS_URLS)
def test_payload_egress_targets_are_blocked(url: str) -> None:
    """No egress target from any case payload is allowlisted for any role."""
    for role in _EGRESS_ROLES:
        with pytest.raises(PolicyBlocked):
            check_egress(role, "GET", url, registry_path=REGISTRY, policy_path=POLICY)


def test_no_unauthorized_tool_calls_after_full_redteam_run(monkeypatch: MonkeyPatch) -> None:
    """Run every case end to end; the unauthorised-call counter stays at zero."""
    _guard(monkeypatch)
    for case in CASE_FILES:
        result = ingest_untrusted(_payload(case))
        commands, tools = executable_requests(result.data.marked())
        assert commands == []
        assert tools == []
        assert result.commands_executed == 0
        assert result.tool_calls_executed == 0
    assert unauthorized_tool_call_count() == 0


def test_no_secrets_in_audit_log_from_case_payloads(monkeypatch: MonkeyPatch) -> None:
    """Writing every case payload through the audit log leaves no secret text."""
    _guard(monkeypatch)
    fake = "ghp_" + ("a" * 36)
    with TemporaryDirectory() as directory:
        log = Path(directory) / "audit.log"
        for case in CASE_FILES:
            write_audit_log(log, _payload(case))
        write_audit_log(log, f"upstream body contained {fake}")
        text = log.read_text(encoding="utf-8")
    assert fake not in text
    assert "[REDACTED]" in text
    assert not _SECRET_IN_PAYLOAD.search(text)


def test_marker_coverage_of_payload_families() -> None:
    """Each family of the frozen list is represented in the corpus."""
    corpus = "\n".join(_payload(case) for case in CASE_FILES)
    assert "tool_call" in corpus
    assert "curl" in corpus or "wget" in corpus or "powershell" in corpus
    assert "GITHUB_TOKEN" in corpus or "OPENAI_API_KEY" in corpus
    assert "budget" in corpus or "$25" in corpus
    assert "default policy" in corpus or "source registry" in corpus
    assert "\u0441url" in corpus or "\u202e" in corpus or "\u200b" in corpus


class FailTransport(httpx.BaseTransport):
    """Any request that reaches a transport means egress was not blocked."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"HTTP request was sent: {request.url}")


def test_http_get_egress_through_guarded_client() -> None:
    """A payload URL used as an http_get argument cannot reach the network."""

    def hook(request: httpx.Request) -> None:
        check_egress(
            "CollectorAgent",
            request.method,
            str(request.url),
            registry_path=REGISTRY,
            policy_path=POLICY,
        )

    for url in _EGRESS_URLS:
        with httpx.Client(transport=FailTransport(), event_hooks={"request": [hook]}) as client:
            with pytest.raises(PolicyBlocked):
                client.get(url)


def test_case_payloads_are_small_local_fixtures() -> None:
    """No fixture may exceed ~100 KB; no live endpoints are used."""
    for case in CASE_FILES:
        assert case.stat().st_size < 100_000, case.name


def test_ingest_output_is_json_safe() -> None:
    """Fenced output must round-trip through JSON for the Q20 artifacts."""
    for case in CASE_FILES:
        fenced = ingest_untrusted(_payload(case)).data.marked()
        encoded = json.dumps({"case": case.name, "fenced": fenced})
        assert json.loads(encoded)["case"] == case.name
