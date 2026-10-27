"""Version 1.0 task, record, evidence, trend, and report contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"
HTTPS_URL_PATTERN = r"^https://\S+$"
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class LicenseStatus(StrEnum):
    METADATA_ONLY = "metadata_only"
    REUSABLE_EXCERPT = "reusable_excerpt"
    FULLTEXT_ALLOWED = "fulltext_allowed"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


class SourceId(StrEnum):
    OPENALEX = "openalex"
    CROSSREF = "crossref"
    ARXIV = "arxiv"
    GITHUB = "github"
    HN = "hn"
    RSS = "rss"
    SEC = "sec"
    CNINFO = "cninfo"


class SourceFamily(StrEnum):
    ACADEMIC = "academic"
    CODE = "code"
    COMMUNITY = "community"
    NEWS = "news"


class QualityTier(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class TaskStatus(StrEnum):
    """Section 0 task states, spelled as the section 4.2 JSON example."""

    WAITING = "waiting"
    READY = "ready"
    RUNNING = "running"
    SUCCESS = "success"
    RETRYABLE_ERROR = "retryable_error"
    FATAL_ERROR = "fatal_error"
    POLICY_BLOCKED = "policy_blocked"


class TriggerType(StrEnum):
    DEPENDENCY = "dependency"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReportKind(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    TOPIC = "topic"


NonEmptyStr = Annotated[str, Field(min_length=1)]


def _json_int(value: Any) -> Any:
    # JSON Schema integer is not a string, float, or boolean. Pydantic would coerce those.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer required")
    return value


def _json_number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("number required")
    return value


def _json_bool(value: Any) -> Any:
    if not isinstance(value, bool):
        raise ValueError("boolean required")
    return value


def _json_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("datetime must be a string")
    return value


JsonInt = Annotated[int, BeforeValidator(_json_int)]
JsonNumber = Annotated[float, BeforeValidator(_json_number)]
JsonBool = Annotated[bool, BeforeValidator(_json_bool)]
JsonDatetime = Annotated[AwareDatetime, BeforeValidator(_json_datetime)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskInput(_Model):
    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str = Field(min_length=1)


class Trigger(_Model):
    type: TriggerType
    at: JsonDatetime


class Budget(_Model):
    max_seconds: JsonInt = Field(ge=0)
    max_usd: JsonNumber = Field(ge=0)
    max_tokens: JsonInt = Field(ge=0)


class TaskEnvelope(_Model):
    task_id: str = Field(min_length=1)
    run_id: str = Field(pattern=UUID_PATTERN)
    attempt: JsonInt = Field(ge=1, le=3)
    trigger: Trigger
    inputs: list[TaskInput]
    params: dict[str, Any]
    budget: Budget
    policy_version: str = Field(min_length=1)
    schema_version: Literal["1.0"]


class TaskArtifact(_Model):
    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    rows: JsonInt = Field(ge=0)


class TaskResult(_Model):
    task_id: str = Field(min_length=1)
    run_id: str = Field(pattern=UUID_PATTERN)
    status: TaskStatus
    artifacts: list[TaskArtifact]
    metrics: dict[str, Any]
    warnings: list[str]
    errors: list[str]
    next_tasks: list[NonEmptyStr]


class RawRecord(_Model):
    schema_version: Literal["1.0"]
    source_id: SourceId
    source_record_id: str = Field(min_length=1)
    canonical_url: str = Field(pattern=HTTPS_URL_PATTERN)
    title: str = Field(min_length=1)
    abstract_or_excerpt: str
    published_at: JsonDatetime | None
    published_at_missing_reason: str | None = Field(default=None, min_length=1)
    collected_at: JsonDatetime
    language: str = Field(min_length=1)
    license_status: LicenseStatus
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    policy_snapshot_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _null_published_at_needs_reason(self) -> Self:
        # Section 4: published_at may be null only with a missing reason.
        if self.published_at is None and not self.published_at_missing_reason:
            raise ValueError("published_at null requires published_at_missing_reason")
        return self


class EvidenceItem(_Model):
    evidence_id: str = Field(min_length=1)
    canonical_id: str = Field(min_length=1)
    source_family: SourceFamily
    source_url: str = Field(pattern=HTTPS_URL_PATTERN)
    source_published_at: JsonDatetime
    collected_at: JsonDatetime
    normalized_excerpt: str = Field(min_length=1)
    quote_length: JsonInt = Field(ge=0)
    license_status: LicenseStatus
    quality_tier: QualityTier
    sha256: str = Field(pattern=SHA256_PATTERN)


class Trend(_Model):
    trend_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    window_days: Literal[7, 28]
    score: JsonNumber
    confidence: Confidence
    source_families: list[SourceFamily]
    claim: str = Field(min_length=1)
    supporting_evidence_ids: list[NonEmptyStr]
    contrary_evidence_ids: list[NonEmptyStr]
    numeric_checks_passed: JsonBool
    last_verified_at: JsonDatetime

    @field_validator("source_families")
    @classmethod
    def _unique_families(cls, value: list[SourceFamily]) -> list[SourceFamily]:
        if len(set(value)) != len(value):
            raise ValueError("source_families must be unique")
        return value

    @model_validator(mode="after")
    def _high_confidence_needs_three_families(self) -> Self:
        # Section 4 hard rule checked here: source families >= 3.
        if self.confidence is Confidence.HIGH and len(self.source_families) < 3:
            raise ValueError("confidence=high requires at least 3 source families")
        return self


class Report(_Model):
    schema_version: Literal["1.0"]
    report_id: str = Field(min_length=1)
    report_kind: ReportKind
    content_uri: str = Field(min_length=1)
    qa_manifest_uri: str = Field(min_length=1)
    evidence_snapshot_uri: str = Field(min_length=1)
    published_at: JsonDatetime
    generated_by_ai: JsonBool
