"""Q20 red-team harness: per-case guard replay shared by tests and the artifact script."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from techpulse.governance.egress import check_egress, load_domain_allowlist
from techpulse.governance.policy import (
    Policy,
    PolicyBlocked,
    executable_requests,
    ingest_untrusted,
    load_policy,
    normalize_domain,
    perform_tool_call,
    redact_secrets,
    unauthorized_tool_call_count,
    write_audit_log,
)

# URL shapes a payload uses to describe an egress target.
_URL_IN_PAYLOAD = re.compile(r"(?i)\b(?:https?|ftp|file)://[^\s'\"<>{}]+")
# Bare dotted hosts that remain after the URLs themselves are removed.
_HOST_IN_PAYLOAD = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])\.)+[a-z]{2,}\b")
# Tool names a payload asks an agent to call.
_TOOL_CALL_NAME = re.compile(r'"(?:tool_call|function_call)","name":"([A-Za-z0-9_]+)"')

# Secret-shaped strings that must never survive redaction.
SECRET_SHAPES = re.compile(
    r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,}"
)


def tool_names_in(text: str) -> set[str]:
    """Tool names a payload asks an agent to call."""
    return set(_TOOL_CALL_NAME.findall(text))


def extract_egress_targets(payload: str) -> list[str]:
    """Every URL and bare host a payload carries, deduplicated, in first-seen order."""
    targets: list[str] = []
    seen: set[str] = set()

    def add(target: str) -> None:
        key = target.lower()
        if key not in seen:
            seen.add(key)
            targets.append(target)

    remainder = payload
    for match in _URL_IN_PAYLOAD.finditer(payload):
        url = match.group(0).rstrip(".,;:)]")
        add(url)
        remainder = remainder.replace(match.group(0), " ", 1)
    for match in _HOST_IN_PAYLOAD.finditer(remainder):
        host = match.group(0).rstrip(".")
        add(f"https://{host}/")
    return targets


def target_host(target: str) -> str | None:
    """Normalized allowlist key for a target, or None when it can never be allowlisted."""
    try:
        parsed = urlsplit(target)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if parsed.hostname is None:
        return None
    try:
        return normalize_domain(parsed.hostname)
    except ValueError:
        return None


@dataclass
class CaseReport:
    """Guard replay result for one red-team case file."""

    case: str
    category: str
    status: str
    blocked_at_ingest: bool = False
    trusted_view_commands: int = 0
    trusted_view_tool_calls: int = 0
    forbidden_tool_call_attempts: int = 0
    forbidden_tool_calls_denied: int = 0
    egress_targets: int = 0
    egress_check_attempts: int = 0
    egress_checks_blocked: int = 0
    secrets_leaked: bool = False

    def passed(self) -> bool:
        return (
            self.trusted_view_commands == 0
            and self.trusted_view_tool_calls == 0
            and self.forbidden_tool_calls_denied == self.forbidden_tool_call_attempts
            and self.egress_checks_blocked == self.egress_check_attempts
            and not self.secrets_leaked
        )


@dataclass
class SuiteReport:
    """Aggregate guard replay result for the whole red-team corpus."""

    cases_total: int
    cases_passed: int
    cases_failed: int
    cases_blocked_at_ingest: int
    egress_targets: int
    egress_checks_blocked: int
    forbidden_tool_calls_denied: int
    secrets_leaked: int
    unauthorized_tool_calls_executed: int
    status: str
    cases: list[CaseReport]


def run_case(
    case: Path,
    policy: Policy,
    *,
    policy_path: Path,
    registry_path: Path,
) -> CaseReport:
    """Replay one payload through the S20 guards and record what each check did."""
    payload = case.read_text(encoding="utf-8")
    roles = sorted(policy.roles)
    allowlist = load_domain_allowlist(registry_path=registry_path, policy_path=policy_path)
    report = CaseReport(case=case.name, category=case.name.split("_", 1)[0], status="fail")

    try:
        fenced = ingest_untrusted(payload).data.marked()
    except PolicyBlocked:
        report.blocked_at_ingest = True
        report.status = "blocked_at_ingest"
        return report

    commands, tools = executable_requests(fenced)
    report.trusted_view_commands = len(commands)
    report.trusted_view_tool_calls = len(tools)

    for tool in sorted(tool_names_in(payload) | tool_names_in(fenced)):
        for role in roles:
            if tool in policy.grant(role).tools:
                # Granted by policy independent of any payload; out of scope.
                continue
            report.forbidden_tool_call_attempts += 1
            try:
                perform_tool_call(
                    role,
                    tool,
                    {"record_id": "x", "body": fenced},
                    registry_path=registry_path,
                    policy_path=policy_path,
                )
            except PolicyBlocked:
                report.forbidden_tool_calls_denied += 1

    targets = extract_egress_targets(payload)
    report.egress_targets = len(targets)
    for target in targets:
        host = target_host(target)
        if host is not None and host in allowlist:
            # Reachable by design for allowlisted_get roles; not an egress violation.
            continue
        for role in roles:
            report.egress_check_attempts += 1
            try:
                check_egress(
                    role,
                    "GET",
                    target,
                    registry_path=registry_path,
                    policy_path=policy_path,
                )
            except PolicyBlocked:
                report.egress_checks_blocked += 1

    with TemporaryDirectory() as directory:
        log = Path(directory) / "audit.log"
        write_audit_log(log, payload)
        leaked = bool(SECRET_SHAPES.search(log.read_text(encoding="utf-8")))
    report.secrets_leaked = leaked or bool(SECRET_SHAPES.search(redact_secrets(fenced)))

    report.status = "pass" if report.passed() else "fail"
    return report


def run_suite(
    cases_dir: Path,
    *,
    policy_path: Path,
    registry_path: Path,
) -> SuiteReport:
    """Replay every case file through the S20 guards and aggregate the counts."""
    policy = load_policy(policy_path)
    cases = sorted(cases_dir.glob("*.txt"))
    reports = [
        run_case(case, policy, policy_path=policy_path, registry_path=registry_path)
        for case in cases
    ]
    failed = [report for report in reports if report.status != "pass"]
    unauthorized = unauthorized_tool_call_count()
    return SuiteReport(
        cases_total=len(reports),
        cases_passed=len(reports) - len(failed),
        cases_failed=len(failed),
        cases_blocked_at_ingest=sum(1 for r in reports if r.blocked_at_ingest),
        egress_targets=sum(r.egress_targets for r in reports),
        egress_checks_blocked=sum(r.egress_checks_blocked for r in reports),
        forbidden_tool_calls_denied=sum(r.forbidden_tool_calls_denied for r in reports),
        secrets_leaked=sum(1 for r in reports if r.secrets_leaked),
        unauthorized_tool_calls_executed=unauthorized,
        status="pass" if not failed and unauthorized == 0 else "fail",
        cases=reports,
    )


def suite_document(report: SuiteReport) -> dict[str, object]:
    """JSON-ready view of a suite report."""
    return asdict(report)
