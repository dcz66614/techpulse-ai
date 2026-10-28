"""Q10 fact, citation, copyright, and format checks.

The checks read the R10 draft, the E10 claim-to-evidence mapping, stored
feature numbers and dates, and stored raw records or snapshots. They do not
call a model, a network, or a paid API, and they do not write evidence.

A draft is signed only when every gate passes: claim citation coverage at
least 95%, URL traceability exactly 100% of the displayed public claims,
numeric and date consistency at least 98%, unknown or blocked license
releases at 0, no PII in the draft, and the draft text matching the claim
list. A failure is routed back to R10, E10, or D21. The third failure of a
run is marked for human review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from techpulse.governance.pii import contains_pii
from techpulse.workflows.activities import LocalObjectStore, ObjectStore, split_object_uri

CITATION_MIN_MILLI = 950
URL_MIN_MILLI = 1000
NUMERIC_MIN_MILLI = 980
FORMAT_MIN_MILLI = 1000
_PUBLIC_LICENSE = frozenset({"metadata_only", "reusable_excerpt", "fulltext_allowed"})
_FACT_KINDS = frozenset({"fact", "hypothesis"})
_RELEASE_STATUS = frozenset({"success", "no_material_update"})
_WINDOW = {
    "growth_7d": ("window_7d_start", "window_7d_end"),
    "growth_7d_unknown": ("window_7d_start", "window_7d_end"),
    "diversity_7d": ("window_7d_start", "window_7d_end"),
    "quality_7d": ("window_7d_start", "window_7d_end"),
    "developer_7d": ("window_7d_start", "window_7d_end"),
    "novelty_7d": ("window_7d_start", "window_7d_end"),
    "persistence_28d": ("window_28d_start", "window_28d_end"),
}
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_URL = re.compile(r"^https://\S+$")
_URL_IN_TEXT = re.compile(r"https?://[^\s|）】>\"')]+")
_DOI_IN_TEXT = re.compile(r"\b10\.\d{4,9}/[^\s|）】>\"')]+")
_TIME_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_DDL = """
CREATE TABLE IF NOT EXISTS qa_runs (
    run_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    status TEXT NOT NULL,
    release_status TEXT NOT NULL,
    qa_pass INTEGER NOT NULL,
    signature TEXT,
    attempt INTEGER NOT NULL,
    human_review_queue INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_checks (
    run_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    value REAL NOT NULL,
    threshold REAL NOT NULL,
    passed INTEGER NOT NULL,
    numerator INTEGER,
    denominator INTEGER,
    PRIMARY KEY (run_id, check_name)
);
CREATE TABLE IF NOT EXISTS qa_blockers (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    code TEXT NOT NULL,
    route TEXT NOT NULL,
    claim_id TEXT,
    reason TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class QaResult:
    status: str
    qa_pass: bool
    document: dict[str, object]
    metrics: dict[str, object]


@dataclass(frozen=True)
class _RawIndex:
    by_key: dict[tuple[str, str], tuple[Mapping[str, object], ...]]
    by_hash: dict[str, tuple[Mapping[str, object], ...]]


@dataclass(frozen=True)
class _Unit:
    ok: bool
    route: str
    claim_id: str
    reason: str


def review_report(
    report: Mapping[str, object],
    evidence_pack: Mapping[str, object],
    *,
    markdown: str,
    features: Sequence[Mapping[str, object]],
    raw_records: Sequence[Mapping[str, object]],
    snapshots: Mapping[str, bytes] | None = None,
    run_id: str | None = None,
    attempt: int = 1,
) -> QaResult:
    """Score one draft. Inputs are read only."""
    if attempt < 1:
        raise ValueError("attempt must be positive")
    checked_run = run_id if run_id is not None else _text(report.get("run_id"))
    report_id = _text(report.get("report_id")) or (
        f"report-{checked_run}" if checked_run else "report"
    )
    stored_snapshots = snapshots if snapshots is not None else {}
    evidence = _index_evidence(evidence_pack)
    e10_claims = _index_claims(evidence_pack)
    topics = _topics(evidence_pack)
    feature_rows = _index_features(features)
    raw_index = _index_raw(raw_records)
    traced = _traced_evidence(evidence, raw_index, stored_snapshots)
    claims = _claim_rows(report)
    blockers: list[dict[str, object]] = []
    citation_hits = 0
    url_hits = 0
    numeric_hits = 0
    numeric_total = 0
    seen: set[tuple[str, str, str, str]] = set()

    def add(code: str, route: str, claim_id: str, reason: str) -> None:
        key = (code, route, claim_id, reason)
        if key in seen:
            return
        seen.add(key)
        blockers.append(
            {"code": code, "route": route, "claim_id": claim_id or None, "reason": reason}
        )

    for claim in claims:
        claim_id = _text(claim.get("claim_id")) or ""
        cited, cite_route, cite_reason = _citation(claim, evidence, e10_claims)
        if cited:
            citation_hits += 1
        else:
            add("citation_coverage", cite_route, claim_id, cite_reason)
        url_ok, url_route, url_reason = _claim_url(claim, traced)
        if url_ok:
            url_hits += 1
        else:
            add("url_traceability", url_route, claim_id, url_reason)
        for unit in _units(claim, evidence, e10_claims, topics, feature_rows, raw_index):
            numeric_total += 1
            if unit.ok:
                numeric_hits += 1
            else:
                add("numeric_date_consistency", unit.route, unit.claim_id, unit.reason)
    orphans = _orphan_urls(markdown, claims, evidence)
    for url in orphans:
        add("url_traceability", "R10", "", f"url is not in the evidence pack: {url}")
    unknown = _unknown_releases(claims, report, markdown, evidence, raw_index)
    for claim_id, reason in unknown:
        add("unknown_license_external_release", "E10", claim_id, reason)
    pii_findings = _pii_findings(markdown, claims)
    if pii_findings:
        add("pii", "D21", "", "draft contains pii")
    format_ok, format_reasons = _format_problems(report, markdown, claims)
    for claim_id, reason in format_reasons:
        add("format", "R10", claim_id, reason)

    blockers.sort(key=lambda item: (str(item["code"]), str(item["claim_id"]), str(item["reason"])))
    checks = {
        "citation_coverage": _rate(citation_hits, len(claims), CITATION_MIN_MILLI, minimum=0.95),
        "url_traceability": _rate(url_hits, len(claims) + len(orphans), URL_MIN_MILLI, minimum=1.0),
        "numeric_date_consistency": _rate(
            numeric_hits, numeric_total, NUMERIC_MIN_MILLI, minimum=0.98
        ),
        "unknown_license_external_release": _count(len(unknown), maximum=0),
        "pii": _count(pii_findings, maximum=0),
        "format": _rate(1 if format_ok else 0, 1, FORMAT_MIN_MILLI, minimum=1.0),
    }
    qa_pass = all(item["passed"] is True for item in checks.values()) and not blockers
    signature = _signature(checked_run, report_id, checks) if qa_pass else None
    human_review = (not qa_pass) and attempt >= 3
    status = "pass" if qa_pass else "fail"
    release_status = "PASS" if qa_pass else "BLOCKED"
    routes = _routes(blockers)
    document: dict[str, object] = {
        "schema_version": "1.0",
        "ticket": "Q10",
        "run_id": checked_run,
        "report_id": report_id,
        "status": status,
        "release_status": release_status,
        "qa_pass": qa_pass,
        "signed": qa_pass,
        "signature": signature,
        "attempt": attempt,
        "human_review_queue": human_review,
        "checks": checks,
        "blockers": blockers,
        "routes": routes,
        "llm_calls": 0,
        "paid_api_calls": 0,
        "third_party_writes": 0,
        "evidence_modified": False,
    }
    metrics: dict[str, object] = {
        "status": status,
        "qa_pass": qa_pass,
        "release_status": release_status,
        "signed": qa_pass,
        "citation_coverage": checks["citation_coverage"]["value"],
        "url_traceability": checks["url_traceability"]["value"],
        "numeric_date_consistency": checks["numeric_date_consistency"]["value"],
        "unknown_license_external_release": len(unknown),
        "pii_findings": pii_findings,
        "format_passed": format_ok,
        "public_claims": len(claims),
        "blockers": len(blockers),
        "routes": routes,
        "attempt": attempt,
        "human_review_queue": human_review,
        "llm_calls": 0,
        "paid_api_calls": 0,
        "third_party_writes": 0,
        "evidence_modified": False,
    }
    return QaResult(status, qa_pass, document, metrics)


def run_qa(
    report: Mapping[str, object],
    evidence_pack: Mapping[str, object],
    *,
    markdown: str,
    features: Sequence[Mapping[str, object]],
    raw_records: Sequence[Mapping[str, object]],
    run_id: str,
    artifacts_dir: Path,
    store: ObjectStore | None = None,
    database: Path | None = None,
    snapshots: Mapping[str, bytes] | None = None,
    attempt: int = 1,
) -> QaResult:
    """Write ``{run_id}/Q10`` artifacts. Evidence keys are not written."""
    checked = _run_id(run_id)
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    loaded = dict(snapshots or {})
    for record in raw_records:
        uri = _text(record.get("raw_uri"))
        if uri is None or uri in loaded:
            continue
        body = _read_snapshot(object_store, uri)
        if body is not None:
            loaded[uri] = body
    result = review_report(
        report,
        evidence_pack,
        markdown=markdown,
        features=features,
        raw_records=raw_records,
        snapshots=loaded,
        run_id=checked,
        attempt=attempt,
    )
    database_path = (
        database if database is not None else artifacts_dir / checked / "Q10" / "qa.sqlite"
    )
    _save(database_path, result)
    body = _json_bytes(result.document)
    object_store.put(f"{checked}/Q10/qa_manifest.json", body)
    manifest: dict[str, object] = {
        "ticket": "Q10",
        "run_id": checked,
        "status": result.status,
        "qa_pass": result.qa_pass,
        "release_status": result.document["release_status"],
        "qa_manifest": "qa_manifest.json",
        "qa_manifest_sha256": hashlib.sha256(body).hexdigest(),
        "signature": result.document["signature"],
        "database": database_path.name,
        "attempt": attempt,
        "human_review_queue": result.document["human_review_queue"],
        "evidence_modified": False,
    }
    object_store.put(f"{checked}/Q10/manifest.json", _json_bytes(manifest))
    object_store.put(f"{checked}/Q10/metrics.json", _json_bytes(result.metrics))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Load a manifest and write Q10. ``--strict`` exits 1 when the draft fails."""
    parser = argparse.ArgumentParser(prog="techpulse.reports.qa")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = _from_manifest(Path(args.manifest))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"qa: {exc}", file=sys.stderr)
        return 2
    if args.strict and not result.qa_pass:
        return 1
    return 0


def _from_manifest(path: Path) -> QaResult:
    payload = _object(_load_json(path))
    if payload is None:
        raise ValueError("manifest must be an object")
    root = path.parent
    run_id = _required_text(payload, "run_id")
    artifacts_dir = _resolve(root, _required_text(payload, "artifacts_dir"))
    report = _object(_load_json(_resolve(root, _required_text(payload, "report_claims"))))
    evidence_pack = _object(_load_json(_resolve(root, _required_text(payload, "evidence_pack"))))
    if report is None or evidence_pack is None:
        raise ValueError("report_claims and evidence_pack must be objects")
    markdown = _resolve(root, _required_text(payload, "draft")).read_text(encoding="utf-8")
    features = _rows(_load_table(_resolve(root, _required_text(payload, "features"))))
    raw_records = _rows(_load_table(_resolve(root, _required_text(payload, "raw_records"))))
    attempt = payload.get("attempt", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise ValueError("attempt must be an integer")
    database_value = payload.get("database")
    database = (
        None if database_value is None else _resolve(root, _required_text(payload, "database"))
    )
    return run_qa(
        report,
        evidence_pack,
        markdown=markdown,
        features=features,
        raw_records=raw_records,
        run_id=run_id,
        artifacts_dir=artifacts_dir,
        database=database,
        attempt=attempt,
    )


def _citation(
    claim: Mapping[str, object],
    evidence: Mapping[str, Mapping[str, object]],
    e10_claims: Mapping[str, Mapping[str, object]],
) -> tuple[bool, str, str]:
    evidence_ids = _strings(claim.get("evidence_ids"))
    if not evidence_ids:
        return False, "R10", "claim has no evidence id"
    if any(evidence_id not in evidence for evidence_id in evidence_ids):
        return False, "E10", "evidence id is not in the evidence pack"
    kind = _text(claim.get("kind")) or "fact"
    if kind not in _FACT_KINDS:
        return True, "", ""
    source_claim_id = _text(claim.get("source_claim_id"))
    source = None if source_claim_id is None else e10_claims.get(source_claim_id)
    if source is None:
        return False, "E10", "source claim is not in the evidence pack"
    support = set(_strings(source.get("supporting_evidence_ids")))
    if any(evidence_id not in support for evidence_id in evidence_ids):
        return False, "E10", "evidence id is not in the claim-to-evidence mapping"
    return True, "", ""


def _claim_url(
    claim: Mapping[str, object],
    traced: Mapping[str, Mapping[str, object]],
) -> tuple[bool, str, str]:
    evidence_ids = _strings(claim.get("evidence_ids"))
    if not evidence_ids:
        return False, "R10", "claim has no traceable source url"
    if any(evidence_id not in traced for evidence_id in evidence_ids):
        return False, "E10", "source url is not traceable to a stored raw record"
    text = _text(claim.get("text")) or ""
    cited = [traced[evidence_id] for evidence_id in evidence_ids]
    urls = set(_urls_in(cited))
    dois = _doi_set(cited)
    for url in _urls_in(text):
        if url not in urls:
            return False, "R10", "url is not in the cited evidence"
    for doi in _dois_in(text):
        if doi not in dois:
            return False, "R10", "doi is not in the cited evidence"
    return True, "", ""


def _units(
    claim: Mapping[str, object],
    evidence: Mapping[str, Mapping[str, object]],
    e10_claims: Mapping[str, Mapping[str, object]],
    topics: Mapping[str, str],
    features: Mapping[str, Mapping[str, object]],
    raw_index: _RawIndex,
) -> list[_Unit]:
    claim_id = _text(claim.get("claim_id")) or ""
    text = _text(claim.get("text")) or ""
    template = _text(claim.get("template")) or ""
    kind = _text(claim.get("kind")) or "fact"
    source_claim_id = _text(claim.get("source_claim_id"))
    source = None if source_claim_id is None else e10_claims.get(source_claim_id)
    trend_id = _text(claim.get("trend_id"))
    topic = None if trend_id is None else topics.get(trend_id)
    feature = None if topic is None else features.get(topic)
    units = [
        _number_unit(claim_id, name, value, text, feature, source)
        for name, value in _numbers(claim)
    ]
    if kind in _FACT_KINDS and template in _WINDOW:
        units.append(_window_unit(claim_id, template, feature, source))
    cited = [
        evidence[evidence_id]
        for evidence_id in _strings(claim.get("evidence_ids"))
        if evidence_id in evidence
    ]
    units.extend(_text_date_units(claim_id, text, feature, source, cited))
    for evidence_id in _strings(claim.get("evidence_ids")):
        units.append(_evidence_date_unit(claim_id, evidence_id, evidence, raw_index))
    return units


def _number_unit(
    claim_id: str,
    name: str,
    value: int | float,
    text: str,
    feature: Mapping[str, object] | None,
    source: Mapping[str, object] | None,
) -> _Unit:
    if feature is None or name not in feature:
        return _unit(False, "E10", claim_id, "stored feature has no field for the claim number")
    feature_value = feature.get(name)
    if not _same_number(value, feature_value):
        return _unit(False, "R10", claim_id, "claim number does not match the stored feature")
    inputs = _object(None if source is None else source.get("inputs")) or {}
    stored = inputs.get(name)
    if not _same_number(value, stored):
        if _same_number(stored, feature_value):
            return _unit(False, "R10", claim_id, "claim number does not match the evidence claim")
        return _unit(
            False, "E10", claim_id, "evidence claim number does not match the stored feature"
        )
    if not _number_in_text(value, text):
        return _unit(False, "R10", claim_id, "claim text does not contain the stored number")
    return _unit(True, "", claim_id, "")


def _window_unit(
    claim_id: str,
    template: str,
    feature: Mapping[str, object] | None,
    source: Mapping[str, object] | None,
) -> _Unit:
    start_key, end_key = _WINDOW[template]
    feature_start = None if feature is None else feature.get(start_key)
    feature_end = None if feature is None else feature.get(end_key)
    claim_start = None if source is None else source.get("window_start")
    claim_end = None if source is None else source.get("window_end")
    if _same_time(claim_start, feature_start) and _same_time(claim_end, feature_end):
        return _unit(True, "", claim_id, "")
    return _unit(False, "E10", claim_id, "claim window does not match the stored feature dates")


def _text_date_units(
    claim_id: str,
    text: str,
    feature: Mapping[str, object] | None,
    source: Mapping[str, object] | None,
    evidence: Sequence[Mapping[str, object]],
) -> list[_Unit]:
    allowed: list[object] = []
    if feature is not None:
        for key, value in feature.items():
            if key == "as_of" or key.endswith("_start") or key.endswith("_end"):
                allowed.append(value)
    if source is not None:
        allowed.extend((source.get("window_start"), source.get("window_end")))
    for item in evidence:
        allowed.extend((item.get("source_published_at"), item.get("collected_at")))
    units: list[_Unit] = []
    for stamp in _TIME_IN_TEXT.findall(text):
        if any(_same_time(stamp, item) for item in allowed):
            units.append(_unit(True, "", claim_id, ""))
        else:
            units.append(
                _unit(
                    False,
                    "R10",
                    claim_id,
                    "claim text date is not stored on the feature or the evidence",
                )
            )
    return units


def _evidence_date_unit(
    claim_id: str,
    evidence_id: str,
    evidence: Mapping[str, Mapping[str, object]],
    raw_index: _RawIndex,
) -> _Unit:
    item = evidence.get(evidence_id)
    if item is None:
        return _unit(False, "E10", claim_id, "evidence date is not in the pack")
    record = _match_raw(item, raw_index)
    if record is None:
        return _unit(False, "E10", claim_id, "evidence date has no stored raw record")
    if not _same_time(item.get("source_published_at"), record.get("published_at")):
        return _unit(False, "E10", claim_id, "evidence date does not match the stored raw record")
    collected = item.get("collected_at")
    raw_collected = record.get("collected_at")
    if _text(collected) is not None and _text(raw_collected) is not None:
        if not _same_time(collected, raw_collected):
            return _unit(
                False, "E10", claim_id, "collected_at does not match the stored raw record"
            )
    return _unit(True, "", claim_id, "")


def _traced_evidence(
    evidence: Mapping[str, Mapping[str, object]],
    raw_index: _RawIndex,
    snapshots: Mapping[str, bytes],
) -> dict[str, Mapping[str, object]]:
    traced: dict[str, Mapping[str, object]] = {}
    for evidence_id, item in evidence.items():
        if _trace_ok(item, raw_index, snapshots):
            traced[evidence_id] = item
    return traced


def _trace_ok(
    item: Mapping[str, object],
    raw_index: _RawIndex,
    snapshots: Mapping[str, bytes],
) -> bool:
    url = _text(item.get("source_url"))
    digest = _text(item.get("sha256"))
    if (
        url is None
        or _URL.fullmatch(url) is None
        or digest is None
        or _SHA.fullmatch(digest) is None
    ):
        return False
    record = _match_raw(item, raw_index)
    if record is None:
        return False
    raw_uri = _text(record.get("raw_uri"))
    if raw_uri is None:
        return True
    body = snapshots.get(raw_uri)
    if body is None:
        return False
    return _snapshot_matches(body, url, digest)


def _match_raw(item: Mapping[str, object], raw_index: _RawIndex) -> Mapping[str, object] | None:
    url = _text(item.get("source_url"))
    digest = _text(item.get("sha256"))
    if url is None or digest is None:
        return None
    candidates: list[Mapping[str, object]] = []
    source_id = _text(item.get("source_id"))
    source_record_id = _text(item.get("source_record_id"))
    if source_id is not None and source_record_id is not None:
        candidates.extend(raw_index.by_key.get((source_id, source_record_id), ()))
    candidates.extend(raw_index.by_hash.get(digest, ()))
    seen: set[int] = set()
    for record in candidates:
        identity = id(record)
        if identity in seen:
            continue
        seen.add(identity)
        record_url = _text(record.get("canonical_url"))
        record_hash = _text(record.get("content_sha256"))
        if record_url == url and record_hash == digest:
            return record
    return None


def _snapshot_matches(body: bytes, url: str, digest: str) -> bool:
    if hashlib.sha256(body).hexdigest() == digest:
        return True
    try:
        loaded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    row = _object(loaded)
    if row is None:
        return False
    row_url = _text(row.get("canonical_url")) or _text(row.get("source_url"))
    row_hash = _text(row.get("content_sha256")) or _text(row.get("sha256"))
    return row_url == url and row_hash == digest


def _unknown_releases(
    claims: Sequence[Mapping[str, object]],
    report: Mapping[str, object],
    markdown: str,
    evidence: Mapping[str, Mapping[str, object]],
    raw_index: _RawIndex,
) -> list[tuple[str, str]]:
    released: dict[str, str] = {}
    for claim in claims:
        claim_id = _text(claim.get("claim_id")) or ""
        for evidence_id in _strings(claim.get("evidence_ids")):
            released.setdefault(evidence_id, claim_id)
    for link in _mappings(report.get("links")):
        linked_id = _text(link.get("evidence_id"))
        if linked_id is not None:
            released.setdefault(linked_id, "")
    markdown_urls = set(_urls_in(markdown))
    for stored_id, stored in evidence.items():
        url = _text(stored.get("source_url"))
        if url is not None and url in markdown_urls:
            released.setdefault(stored_id, "")
    found: list[tuple[str, str]] = []
    for evidence_id, claim_id in sorted(released.items()):
        released_item = evidence.get(evidence_id)
        if released_item is None:
            continue
        reason = _license_reason(released_item.get("license_status"))
        if reason is None:
            record = _match_raw(released_item, raw_index)
            if record is not None:
                reason = _license_reason(record.get("license_status"))
        if reason is not None:
            found.append((claim_id, reason))
    return found


def _license_reason(value: object) -> str | None:
    status = _text(value)
    if status in _PUBLIC_LICENSE:
        return None
    if status == "blocked":
        return "license_status is blocked"
    return "license_status is unknown"


def _pii_findings(markdown: str, claims: Sequence[Mapping[str, object]]) -> int:
    texts = [markdown]
    texts.extend(_text(claim.get("text")) or "" for claim in claims)
    return sum(1 for text in texts if text and contains_pii(text))


def _format_problems(
    report: Mapping[str, object],
    markdown: str,
    claims: Sequence[Mapping[str, object]],
) -> tuple[bool, list[tuple[str, str]]]:
    reasons: list[tuple[str, str]] = []
    if not isinstance(report.get("claims"), list):
        return False, [("", "report claims are missing")]
    status = report.get("status")
    if status is not None and status not in _RELEASE_STATUS:
        reasons.append(("", "report status cannot be released"))
    if not isinstance(markdown, str):
        reasons.append(("", "draft markdown is missing"))
    for claim in claims:
        claim_id = _text(claim.get("claim_id")) or ""
        text = claim.get("text")
        if not claim_id:
            reasons.append(("", "claim id is missing"))
        if not isinstance(text, str) or text == "":
            reasons.append((claim_id, "claim text is missing"))
        elif text not in markdown:
            reasons.append((claim_id, "claim text is not in the draft"))
    return not reasons, reasons


def _orphan_urls(
    markdown: str,
    claims: Sequence[Mapping[str, object]],
    evidence: Mapping[str, Mapping[str, object]],
) -> list[str]:
    """URLs shown in the draft that are absent from the evidence pack.

    A URL that belongs to an evidence item stays in that claim's denominator
    even when the raw snapshot does not match. It is not dropped, and it is
    not counted a second time.
    """
    claim_text = "\n".join(_text(claim.get("text")) or "" for claim in claims)
    urls = set(_urls_in(evidence.values()))
    dois = _doi_set(evidence.values())
    orphans: list[str] = []
    for token in [*_urls_in(markdown), *_dois_in(markdown)]:
        if token in claim_text or token in urls or token in dois:
            continue
        if token not in orphans:
            orphans.append(token)
    return orphans


def _rate(numerator: int, denominator: int, milli: int, *, minimum: float) -> dict[str, object]:
    if denominator < 0:
        raise ValueError("denominator must be non-negative")
    value = 1.0 if denominator == 0 else numerator / denominator
    return {
        "value": value,
        "threshold": minimum,
        "passed": _meets(numerator, denominator, milli),
        "numerator": numerator,
        "denominator": denominator,
    }


def _count(count: int, *, maximum: int) -> dict[str, object]:
    return {
        "value": count,
        "threshold": maximum,
        "passed": count <= maximum,
        "numerator": count,
        "denominator": None,
    }


def _meets(numerator: int, denominator: int, milli: int) -> bool:
    """True when ``numerator / denominator`` is at least ``milli / 1000``.

    An empty denominator is a pass. There is nothing displayed to fail, and
    the caller keeps unverifiable claims in the denominator when any exist.
    """
    if denominator <= 0:
        return True
    return numerator * 1000 >= denominator * milli


def _signature(
    run_id: str | None, report_id: str, checks: Mapping[str, Mapping[str, object]]
) -> str:
    payload = {"checks": checks, "report_id": report_id, "run_id": run_id, "ticket": "Q10"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _routes(blockers: Sequence[Mapping[str, object]]) -> dict[str, list[str]]:
    routes: dict[str, list[str]] = {}
    for blocker in blockers:
        route = _text(blocker.get("route"))
        reason = _text(blocker.get("reason"))
        if route is None or reason is None:
            continue
        bucket = routes.setdefault(route, [])
        if reason not in bucket:
            bucket.append(reason)
    return {key: routes[key] for key in sorted(routes)}


def _index_evidence(evidence_pack: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    found: dict[str, Mapping[str, object]] = {}
    for item in _mappings(evidence_pack.get("evidence")):
        evidence_id = _text(item.get("evidence_id"))
        if evidence_id is not None:
            found[evidence_id] = item
    return found


def _index_claims(evidence_pack: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    found: dict[str, Mapping[str, object]] = {}
    for claim in _mappings(evidence_pack.get("claims")):
        claim_id = _text(claim.get("claim_id"))
        if claim_id is not None:
            found[claim_id] = claim
    return found


def _topics(evidence_pack: Mapping[str, object]) -> dict[str, str]:
    found: dict[str, str] = {}
    for trend in _mappings(evidence_pack.get("trends")):
        trend_id = _text(trend.get("trend_id"))
        topic = _text(trend.get("topic")) or _text(trend.get("topic_id"))
        if trend_id is not None and topic is not None:
            found[trend_id] = topic
    return found


def _index_features(
    features: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    found: dict[str, Mapping[str, object]] = {}
    for row in features:
        topic = _text(row.get("topic_id")) or _text(row.get("topic"))
        if topic is not None:
            found[topic] = row
    return found


def _index_raw(records: Sequence[Mapping[str, object]]) -> _RawIndex:
    by_key: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    by_hash: dict[str, list[Mapping[str, object]]] = {}
    for record in records:
        source_id = _text(record.get("source_id"))
        source_record_id = _text(record.get("source_record_id"))
        digest = _text(record.get("content_sha256"))
        if source_id is not None and source_record_id is not None:
            by_key.setdefault((source_id, source_record_id), []).append(record)
        if digest is not None:
            by_hash.setdefault(digest, []).append(record)
    return _RawIndex(
        {key: tuple(value) for key, value in by_key.items()},
        {key: tuple(value) for key, value in by_hash.items()},
    )


def _read_snapshot(store: ObjectStore, uri: str) -> bytes | None:
    try:
        _key, line = split_object_uri(uri)
        body = store.get(uri)
    except (OSError, ValueError):
        return None
    if line is None:
        return body
    lines = body.splitlines()
    if line < 1 or line > len(lines):
        return None
    return lines[line - 1]


def _save(database: Path, result: QaResult) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(_DDL)
        document = result.document
        run_id = _text(document.get("run_id")) or ""
        with connection:
            connection.execute("DELETE FROM qa_blockers WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM qa_checks WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM qa_runs WHERE run_id = ?", (run_id,))
            connection.execute(
                """
                INSERT INTO qa_runs (
                    run_id, report_id, status, release_status, qa_pass,
                    signature, attempt, human_review_queue
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _text(document.get("report_id")) or "",
                    result.status,
                    _text(document.get("release_status")) or "",
                    1 if result.qa_pass else 0,
                    document.get("signature"),
                    document.get("attempt"),
                    1 if document.get("human_review_queue") is True else 0,
                ),
            )
            checks = _object(document.get("checks")) or {}
            for name in sorted(checks):
                check = _object(checks.get(name))
                if check is None:
                    continue
                connection.execute(
                    """
                    INSERT INTO qa_checks (
                        run_id, check_name, value, threshold, passed, numerator, denominator
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        name,
                        _float(check.get("value")),
                        _float(check.get("threshold")),
                        1 if check.get("passed") is True else 0,
                        _optional_int(check.get("numerator")),
                        _optional_int(check.get("denominator")),
                    ),
                )
            for blocker in _mappings(document.get("blockers")):
                connection.execute(
                    """
                    INSERT INTO qa_blockers (run_id, code, route, claim_id, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        _text(blocker.get("code")) or "",
                        _text(blocker.get("route")) or "",
                        blocker.get("claim_id"),
                        _text(blocker.get("reason")) or "",
                    ),
                )
    finally:
        connection.close()


def _claim_rows(report: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = report.get("claims")
    if not isinstance(raw, list):
        return []
    rows: list[Mapping[str, object]] = []
    for item in raw:
        row = _object(item)
        rows.append({} if row is None else row)
    return rows


def _numbers(claim: Mapping[str, object]) -> list[tuple[str, int | float]]:
    found: list[tuple[str, int | float]] = []
    for item in _mappings(claim.get("numbers")):
        name = _text(item.get("name"))
        value = item.get("value")
        if name is None or isinstance(value, bool) or not isinstance(value, int | float):
            continue
        if not math.isfinite(float(value)):
            continue
        found.append((name, value))
    return found


def _number_in_text(value: int | float, text: str) -> bool:
    shown = _format_number(value)
    return re.search(rf"(?<![\d.]){re.escape(shown)}(?![\d.])", text) is not None


def _format_number(value: int | float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return format(float(value), ".10g")


def _same_number(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        if not math.isfinite(float(left)) or not math.isfinite(float(right)):
            return False
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
    return False


def _same_time(left: object, right: object) -> bool:
    parsed_left = _parse_time(left)
    parsed_right = _parse_time(right)
    if parsed_left is None or parsed_right is None:
        return False
    return parsed_left == parsed_right


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _urls_in(value: str | Iterable[Mapping[str, object]]) -> list[str]:
    if isinstance(value, str):
        return [_trim_token(match) for match in _URL_IN_TEXT.findall(value)]
    found: list[str] = []
    for item in value:
        url = _text(item.get("source_url"))
        if url is not None and url not in found:
            found.append(url)
    return found


def _dois_in(text: str) -> list[str]:
    return [_trim_token(match).lower() for match in _DOI_IN_TEXT.findall(text)]


def _doi_set(items: Iterable[Mapping[str, object]]) -> set[str]:
    found: set[str] = set()
    for item in items:
        canonical = _text(item.get("canonical_id"))
        if canonical is None:
            continue
        token = canonical.lower()
        if token.startswith("doi:"):
            token = token[4:]
        if _DOI_IN_TEXT.fullmatch(token):
            found.add(token)
    return found


def _trim_token(token: str) -> str:
    return token.rstrip(".,;:)]}")


def _unit(ok: bool, route: str, claim_id: str, reason: str) -> _Unit:
    return _Unit(ok, route, claim_id, reason)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_table(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        rows: list[object] = []
        for line in text.splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows
    return json.loads(text)


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = _text(payload.get(key))
    if value is None:
        raise ValueError(f"manifest field {key} is required")
    return value


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped == "":
        return None
    return stripped


def _object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    found: list[Mapping[str, object]] = []
    for item in value:
        row = _object(item)
        if row is not None:
            found.append(row)
    return found


def _rows(value: object) -> list[Mapping[str, object]]:
    return _mappings(value)


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


if __name__ == "__main__":
    raise SystemExit(main())
