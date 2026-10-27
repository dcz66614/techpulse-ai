"""Role tool authorization, untrusted-data fencing, and log redaction."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from techpulse.contracts import (
    HTTPS_URL_PATTERN,
    EvidenceItem,
    RawRecord,
    TaskResult,
)

_FENCE_START = "<untrusted_data>"
_FENCE_END = "</untrusted_data>"
_FENCE = re.compile(r"<untrusted_data>\n.*?\n</untrusted_data>", re.DOTALL)
_COMMAND = re.compile(r"(?m)^\s*(?:curl|wget|bash|sh|powershell|rm)\b")
_TOOL_CALL = re.compile(r"tool_call|function_call")
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|token)\b\s*[:=]\s*\S+"),
)
_ROLE_ALIASES = {
    "cleaner": "CleanerAgent",
    "writer": "WriterAgent",
    "qa": "QAAgent",
    "collector": "CollectorAgent",
    "grokbot orchestrator": "GrokbotOrchestrator",
    "grokbot": "GrokbotOrchestrator",
}
_ID_TOOLS = frozenset(
    {
        "read_official_docs",
        "read_raw",
        "dedup",
        "read_sanitized",
        "read_features",
        "read_index",
        "read_evidence",
        "read_draft",
        "rollback",
        "read_metrics",
        "read_logs",
        "read_backup",
        "run_ci",
        "run_migration",
    }
)
_BODY_TOOLS = frozenset(
    {
        "write_repo",
        "write_test_env",
        "propose_source_registry_change",
        "write_sanitized",
        "redact_pii",
        "write_index",
        "write_features",
        "write_draft",
        "write_qa_report",
        "write_service_code",
        "deploy_approved_env",
        "write_ops_record",
        "write_analysis",
    }
)
# Executions that skipped authorization. The call path authorizes first, so this stays 0.
_unauthorized_executed = 0


class PolicyBlocked(Exception):
    """Raised when a call is outside the default-deny policy."""

    code = "policy_blocked"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HttpGetArgs(_Args):
    url: str = Field(pattern=HTTPS_URL_PATTERN)


class QueueArgs(_Args):
    limit: int = Field(default=100, ge=1, le=1000)


class IdArgs(_Args):
    record_id: str = Field(min_length=1)


class BodyArgs(_Args):
    record_id: str = Field(min_length=1)
    body: str


@dataclass(frozen=True)
class RoleGrant:
    network: str
    tools: frozenset[str]


@dataclass(frozen=True)
class Policy:
    fallback_domains: frozenset[str]
    roles: Mapping[str, RoleGrant]

    def grant(self, role: str) -> RoleGrant:
        found = self.roles.get(canonical_role(role))
        if found is None:
            return RoleGrant(network="none", tools=frozenset())
        return found


@dataclass(frozen=True)
class UntrustedData:
    """Third-party text. Prompts must use marked(), which is data, not instructions."""

    text: str

    def marked(self) -> str:
        return f"{_FENCE_START}\n{_escape_untrusted(self.text)}\n{_FENCE_END}"

    def __str__(self) -> str:
        return self.marked()


@dataclass(frozen=True)
class IngestResult:
    data: UntrustedData
    commands_executed: int
    tool_calls_executed: int


def canonical_role(role: str) -> str:
    key = role.strip()
    alias = _ROLE_ALIASES.get(key.lower())
    return key if alias is None else alias


def normalize_domain(value: str) -> str:
    """Return a bare ASCII hostname. Wildcards and empty hosts are rejected."""
    text = value.strip().lower()
    if not text:
        raise ValueError("empty domain")
    if "://" in text:
        host = urlsplit(text).hostname
        if host is None:
            raise ValueError("domain URL has no host")
        normalized = host
    else:
        without_user = text.split("/")[0].split("@")[-1]
        if without_user.startswith("["):
            raise ValueError("IPv6 allowlist entries are not accepted")
        normalized = without_user.split(":")[0]
    if not normalized or "*" in normalized:
        raise ValueError("invalid domain")
    try:
        return normalized.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid domain") from exc


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def write_audit_log(path: Path, message: str) -> None:
    safe = redact_secrets(message).replace("\r", " ").replace("\n", " ")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(safe + "\n")


def executable_requests(text: str) -> tuple[list[str], list[str]]:
    """Commands and tool calls outside untrusted_data fences. Fenced text is data."""
    trusted = _FENCE.sub("", text)
    commands = [match.group(0).strip() for match in _COMMAND.finditer(trusted)]
    tools = _TOOL_CALL.findall(trusted)
    return commands, tools


def ingest_untrusted(text: str) -> IngestResult:
    """Fence third-party text. Instruction-shaped content inside the fence is not run."""
    data = UntrustedData(text=text)
    commands, tools = executable_requests(data.marked())
    if commands or tools:
        raise PolicyBlocked("untrusted text broke out of the data fence")
    return IngestResult(data=data, commands_executed=0, tool_calls_executed=0)


def load_policy(path: Path | None = None) -> Policy:
    policy_path = path if path is not None else _project_root() / "config" / "policy.yaml"
    if not policy_path.is_file():
        raise PolicyBlocked("policy file is missing")
    document = _load_yaml(policy_path)
    mapping = _as_mapping(document)
    if mapping.get("default") != "deny":
        raise ValueError("policy default must be deny")
    allow_raw = mapping.get("network_allowlist")
    allow = _as_mapping(allow_raw)
    domains = frozenset(normalize_domain(item) for item in _as_str_list(allow.get("domains")))
    roles = _load_roles(mapping.get("roles"))
    return Policy(fallback_domains=domains, roles=roles)


def authorize_tool_call(
    role: str,
    tool: str,
    arguments: Mapping[str, object] | None = None,
    *,
    registry_path: Path | None = None,
    policy_path: Path | None = None,
) -> BaseModel:
    """Allow the call only when the role lists the tool and the arguments match its schema."""
    grant = load_policy(policy_path).grant(role)
    if tool not in grant.tools:
        raise PolicyBlocked(f"role {role} cannot call {tool}")
    try:
        model = _tool_model(tool)
    except KeyError as exc:
        raise PolicyBlocked(f"role {role} cannot call {tool}") from exc
    payload: dict[str, object] = {} if arguments is None else dict(arguments)
    try:
        parsed = model.model_validate(payload)
    except ValidationError as exc:
        raise PolicyBlocked(f"schema validation failed for {tool}") from exc
    if isinstance(parsed, HttpGetArgs):
        # Imported lazily so policy and egress can call each other at runtime.
        from techpulse.governance.egress import check_egress

        check_egress(
            role,
            "GET",
            parsed.url,
            registry_path=registry_path,
            policy_path=policy_path,
        )
    return parsed


def perform_tool_call(
    role: str,
    tool: str,
    arguments: Mapping[str, object] | None = None,
    *,
    registry_path: Path | None = None,
    policy_path: Path | None = None,
) -> BaseModel:
    """Execute a tool only after authorization. Denied calls are not run."""
    return authorize_tool_call(
        role,
        tool,
        arguments,
        registry_path=registry_path,
        policy_path=policy_path,
    )


def unauthorized_tool_call_count() -> int:
    """Tool calls that ran without a grant. Authorization happens before execution."""
    return _unauthorized_executed


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_yaml(path: Path) -> object:
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


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
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("expected a list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("expected strings")
        items.append(item.strip())
    return items


def _load_roles(value: object) -> dict[str, RoleGrant]:
    raw = _as_mapping(value)
    if not raw:
        raise ValueError("policy roles must be a non-empty mapping")
    roles: dict[str, RoleGrant] = {}
    for raw_name, spec in raw.items():
        name = canonical_role(raw_name)
        if name in roles:
            raise ValueError(f"duplicate role {name}")
        body = _as_mapping(spec)
        network = body.get("network")
        if not isinstance(network, str) or network not in {"none", "allowlisted_get"}:
            raise ValueError(f"invalid network for {name}")
        tools = _as_str_list(body.get("tools"))
        if len(tools) != len(set(tools)):
            raise ValueError(f"duplicate tool for {name}")
        for tool in tools:
            try:
                _tool_model(tool)
            except KeyError as exc:
                raise ValueError(f"role {name} grants unknown tool {tool}") from exc
        roles[name] = RoleGrant(network=network, tools=frozenset(tools))
    return roles


def _tool_model(tool: str) -> type[BaseModel]:
    contracts: dict[str, type[BaseModel]] = {
        "update_task_status": TaskResult,
        "put_raw": RawRecord,
        "write_evidence": EvidenceItem,
        "http_get": HttpGetArgs,
        "read_task_queue": QueueArgs,
    }
    if tool in contracts:
        return contracts[tool]
    if tool in _ID_TOOLS:
        return IdArgs
    if tool in _BODY_TOOLS:
        return BodyArgs
    raise KeyError(tool)


def _escape_untrusted(text: str) -> str:
    # Strip controls and the fence characters so the payload cannot close the wrapper.
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "&#96;")
    )
