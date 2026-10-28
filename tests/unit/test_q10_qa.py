"""Q10 fact, citation, license, and format checks."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import cast

from techpulse.reports.qa import QaResult, main, review_report, run_qa
from techpulse.workflows.activities import LocalObjectStore

TREND = "trend:topic:rag:7:2026-10-28"
TOPIC = "topic:rag"
START = "2026-10-22T00:00:00Z"
END = "2026-10-29T00:00:00Z"
PUBLISHED = "2026-10-22T00:00:00Z"
COLLECTED = "2026-10-27T00:00:00Z"
URL = "https://example.org/openalex/paper"
EVIDENCE_ID = "ev_support"
SOURCE_CLAIM = "cl_growth"
CLAIM_ID = "rc_growth"
DIGEST = hashlib.sha256(b"openalex:paper").hexdigest()
RAW_URI = "local:q10-unit/D10/raw.ndjson#line=1"
RUN_ID = "q10-unit"


def test_pass_signs_qa_pass() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    before = _frozen(report, pack, features, raw_records)
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert result.status == "pass"
    assert result.qa_pass is True
    assert result.document["signed"] is True
    assert result.document["release_status"] == "PASS"
    assert result.document["qa_pass"] is True
    assert result.document["evidence_modified"] is False
    assert result.document["human_review_queue"] is False
    assert result.metrics["llm_calls"] == 0
    assert result.metrics["paid_api_calls"] == 0
    assert result.metrics["third_party_writes"] == 0
    signature = result.document["signature"]
    assert isinstance(signature, str) and len(signature) == 64
    checks = _checks(result.document)
    assert checks["citation_coverage"]["value"] == 1.0
    assert checks["url_traceability"]["value"] == 1.0
    assert checks["numeric_date_consistency"]["value"] == 1.0
    assert checks["unknown_license_external_release"]["value"] == 0
    assert checks["pii"]["value"] == 0
    assert checks["format"]["passed"] is True
    assert result.document["blockers"] == []
    assert result.document["routes"] == {}
    again = _review(report, pack, markdown, features, raw_records, snapshots)
    assert again.document == result.document
    third = _review(report, pack, markdown, features, raw_records, snapshots, attempt=3)
    assert third.qa_pass is True
    assert third.document["human_review_queue"] is False
    assert _frozen(report, pack, features, raw_records) == before


def test_fail_citation_without_evidence_id_routes_to_r10() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    claim = _claim(report)
    claim["evidence_ids"] = []
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert result.qa_pass is False
    assert result.document["signature"] is None
    assert result.document["release_status"] == "BLOCKED"
    assert _checks(result.document)["citation_coverage"]["passed"] is False
    assert _blocker(result.document, "citation_coverage")["route"] == "R10"
    assert result.document["human_review_queue"] is False
    queued = _review(report, pack, markdown, features, raw_records, snapshots, attempt=3)
    assert queued.document["human_review_queue"] is True
    assert queued.qa_pass is False


def test_fail_citation_unknown_evidence_id_routes_to_e10() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    claim = _claim(report)
    claim["evidence_ids"] = ["ev_missing"]
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert result.qa_pass is False
    assert _checks(result.document)["citation_coverage"]["passed"] is False
    blocker = _blocker(result.document, "citation_coverage")
    assert blocker["route"] == "E10"
    assert blocker["reason"] == "evidence id is not in the evidence pack"


def test_citation_coverage_at_95_percent_keeps_every_claim_in_the_url_denominator() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    claims = [_cloned_claim(report, index) for index in range(20)]
    claims[0]["evidence_ids"] = ["ev_missing"]
    report["claims"] = claims
    markdown = "\n".join(str(claim["text"]) for claim in claims) + "\n" + URL + "\n"
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    citation = _checks(result.document)["citation_coverage"]
    urls = _checks(result.document)["url_traceability"]
    assert citation["numerator"] == 19
    assert citation["denominator"] == 20
    assert citation["passed"] is True
    assert urls["numerator"] == 19
    assert urls["denominator"] == 20
    assert urls["passed"] is False
    assert result.qa_pass is False
    assert result.document["signature"] is None


def test_fail_fictional_url_routes_to_r10() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    claim = _claim(report)
    text = str(claim["text"]) + "来源 https://evil.example/fake。"
    claim["text"] = text
    markdown = text + "\n" + URL + "\n"
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert _checks(result.document)["citation_coverage"]["passed"] is True
    assert _checks(result.document)["url_traceability"]["passed"] is False
    assert _checks(result.document)["numeric_date_consistency"]["passed"] is True
    blocker = _blocker(result.document, "url_traceability")
    assert blocker["route"] == "R10"
    assert blocker["reason"] == "url is not in the cited evidence"
    assert result.qa_pass is False
    directory = _pass_parts()
    directory_markdown = directory[2] + "https://evil.example/directory\n"
    directory_result = _review(
        directory[0],
        directory[1],
        directory_markdown,
        directory[3],
        directory[4],
        directory[5],
    )
    directory_blocker = _blocker(directory_result.document, "url_traceability")
    assert directory_blocker["route"] == "R10"
    assert directory_result.qa_pass is False
    assert _checks(directory_result.document)["url_traceability"]["denominator"] == 2


def test_fail_snapshot_mismatch_routes_to_e10_and_keeps_evidence() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    before = json.dumps(pack, sort_keys=True)
    snapshots = {RAW_URI: b'{"canonical_url":"https://example.org/openalex/paper"}'}
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert _checks(result.document)["citation_coverage"]["passed"] is True
    assert _checks(result.document)["url_traceability"]["passed"] is False
    blocker = _blocker(result.document, "url_traceability")
    assert blocker["route"] == "E10"
    assert result.qa_pass is False
    assert json.dumps(pack, sort_keys=True) == before


def test_fail_numeric_inconsistency_routes_to_r10() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    claim = _claim(report)
    text = _sentence(5, 9)
    claim["text"] = text
    claim["numbers"] = [
        {"name": "growth_previous", "value": 5},
        {"name": "growth_current", "value": 9},
    ]
    markdown = text + "\n" + URL + "\n"
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert _checks(result.document)["numeric_date_consistency"]["passed"] is False
    assert _checks(result.document)["citation_coverage"]["passed"] is True
    assert _checks(result.document)["url_traceability"]["passed"] is True
    blocker = _blocker(result.document, "numeric_date_consistency")
    assert blocker["route"] == "R10"
    assert blocker["reason"] == "claim number does not match the stored feature"
    assert result.qa_pass is False


def test_fail_date_inconsistency_routes_to_e10() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    raw_records[0]["published_at"] = "2020-01-01T00:00:00Z"
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert _checks(result.document)["numeric_date_consistency"]["passed"] is False
    assert _checks(result.document)["url_traceability"]["passed"] is True
    blocker = _blocker(result.document, "numeric_date_consistency")
    assert blocker["route"] == "E10"
    assert blocker["reason"] == "evidence date does not match the stored raw record"
    assert result.qa_pass is False


def test_numeric_consistency_threshold_is_98_percent() -> None:
    passing = _numbered_claims(50, bad=3)
    passed = _review(*passing)
    check = _checks(passed.document)["numeric_date_consistency"]
    assert check["denominator"] == 150
    assert check["numerator"] == 147
    assert check["passed"] is True
    failing = _numbered_claims(50, bad=4)
    failed = _review(*failing)
    rejected = _checks(failed.document)["numeric_date_consistency"]
    assert rejected["numerator"] == 146
    assert rejected["denominator"] == 150
    assert rejected["passed"] is False
    assert failed.qa_pass is False


def test_fail_unknown_license_routes_to_e10() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    _evidence_row(pack)["license_status"] = "unknown"
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert _checks(result.document)["unknown_license_external_release"]["value"] == 1
    assert _checks(result.document)["unknown_license_external_release"]["passed"] is False
    assert _checks(result.document)["citation_coverage"]["passed"] is True
    blocker = _blocker(result.document, "unknown_license_external_release")
    assert blocker["route"] == "E10"
    assert blocker["reason"] == "license_status is unknown"
    assert result.qa_pass is False
    blocked = _pass_parts()
    _evidence_row(blocked[1])["license_status"] = "blocked"
    blocked_result = _review(*blocked)
    assert _blocker(blocked_result.document, "unknown_license_external_release")["reason"] == (
        "license_status is blocked"
    )
    raw_unknown = _pass_parts()
    raw_unknown[4][0]["license_status"] = "unknown"
    raw_result = _review(*raw_unknown)
    assert _blocker(raw_result.document, "unknown_license_external_release")["reason"] == (
        "license_status is unknown"
    )


def test_fail_pii_routes_to_d21() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    markdown = markdown + "联系 user@example.com\n"
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert _checks(result.document)["pii"]["passed"] is False
    assert _checks(result.document)["pii"]["value"] == 1
    assert _checks(result.document)["citation_coverage"]["passed"] is True
    blocker = _blocker(result.document, "pii")
    assert blocker["route"] == "D21"
    assert blocker["reason"] == "draft contains pii"
    assert "user@example.com" not in blocker["reason"]
    assert result.qa_pass is False
    assert result.document["signature"] is None


def test_fail_format_when_claim_text_is_absent_from_the_draft() -> None:
    report, pack, _markdown, features, raw_records, snapshots = _pass_parts()
    result = _review(report, pack, "本草稿没有主张原文。\n", features, raw_records, snapshots)
    assert _checks(result.document)["format"]["passed"] is False
    blocker = _blocker(result.document, "format")
    assert blocker["route"] == "R10"
    assert blocker["reason"] == "claim text is not in the draft"
    assert result.qa_pass is False


def test_clean_r10_fixture_passes() -> None:
    report, pack, plain, features, raw_records, snapshots = _pass_parts()
    markdown = _r10_markdown(_sentence(4, 5))
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    plain_result = _review(report, pack, plain, features, raw_records, snapshots)
    assert result.qa_pass is True
    assert result.document["signed"] is True
    assert result.document["blockers"] == []
    assert result.document["signature"] == plain_result.document["signature"]
    checks = _checks(result.document)
    assert checks["citation_coverage"]["value"] == 1.0
    assert checks["citation_coverage"]["denominator"] == 1
    assert checks["numeric_date_consistency"]["value"] == 1.0
    assert checks["format"]["passed"] is True


def test_unclaimed_sentence_fails_citation_coverage() -> None:
    report, pack, _markdown, features, raw_records, snapshots = _pass_parts()
    clean = _r10_markdown(_sentence(4, 5))
    clean_result = _review(report, pack, clean, features, raw_records, snapshots)
    markdown = _r10_markdown(_sentence(4, 5), extra=("该主题已经进入生产环境。",))
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert result.qa_pass is False
    assert result.document["signature"] is None
    assert result.document["release_status"] == "BLOCKED"
    blocker = _blocker(result.document, "unclaimed_fact")
    assert blocker["route"] == "R10"
    assert blocker["reason"] == "unclaimed_fact"
    citation = _checks(result.document)["citation_coverage"]
    clean_citation = _checks(clean_result.document)["citation_coverage"]
    assert citation["numerator"] == clean_citation["numerator"]
    assert citation["denominator"] == _as_int(clean_citation["denominator"]) + 1
    assert citation["passed"] is False
    numeric = _checks(result.document)["numeric_date_consistency"]
    clean_numeric = _checks(clean_result.document)["numeric_date_consistency"]
    assert numeric["numerator"] == clean_numeric["numerator"]
    assert numeric["denominator"] == clean_numeric["denominator"]
    assert numeric["passed"] is True
    assert _checks(result.document)["format"]["passed"] is True


def test_unclaimed_number_and_date_fail_numeric_consistency() -> None:
    report, pack, _markdown, features, raw_records, snapshots = _pass_parts()
    clean = _r10_markdown(_sentence(4, 5))
    clean_result = _review(report, pack, clean, features, raw_records, snapshots)
    markdown = _r10_markdown(
        _sentence(4, 5),
        extra=("旁注 88，记于 1999-01-01T00:00:00Z。",),
    )
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert result.qa_pass is False
    assert result.document["signature"] is None
    blocker = _blocker(result.document, "unclaimed_fact")
    assert blocker["route"] == "R10"
    assert blocker["reason"] == "unclaimed_fact"
    citation = _checks(result.document)["citation_coverage"]
    clean_citation = _checks(clean_result.document)["citation_coverage"]
    assert citation["numerator"] == clean_citation["numerator"]
    assert citation["denominator"] == _as_int(clean_citation["denominator"]) + 1
    assert citation["passed"] is False
    numeric = _checks(result.document)["numeric_date_consistency"]
    clean_numeric = _checks(clean_result.document)["numeric_date_consistency"]
    assert numeric["numerator"] == clean_numeric["numerator"]
    assert numeric["denominator"] == _as_int(clean_numeric["denominator"]) + 2
    assert numeric["passed"] is False
    assert _checks(result.document)["format"]["passed"] is True


def test_no_material_update_with_no_public_claims_passes() -> None:
    _report, pack, _markdown, features, raw_records, snapshots = _pass_parts()
    report: dict[str, object] = {
        "schema_version": "1.0",
        "ticket": "R10",
        "run_id": RUN_ID,
        "report_id": "report-q10-unit-weekly",
        "status": "no_material_update",
        "claims": [],
        "links": [],
    }
    markdown = "本窗口没有新的证据，不生成事实陈述。\n"
    result = _review(report, pack, markdown, features, raw_records, snapshots)
    assert result.qa_pass is True
    assert result.document["signature"] is not None
    assert _checks(result.document)["citation_coverage"]["denominator"] == 0
    assert _checks(result.document)["url_traceability"]["value"] == 1.0


def test_run_writes_artifacts_and_does_not_modify_evidence() -> None:
    report, pack, markdown, features, raw_records, snapshots = _pass_parts()
    evidence_body = _json_bytes(pack)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = LocalObjectStore(root)
        store.put(f"{RUN_ID}/E10/evidence_pack.json", evidence_body)
        store.put(f"{RUN_ID}/D10/raw.ndjson", snapshots[RAW_URI] + b"\n")
        result = run_qa(
            report,
            pack,
            markdown=markdown,
            features=features,
            raw_records=raw_records,
            run_id=RUN_ID,
            artifacts_dir=root,
            store=store,
        )
        assert store.get(f"local:{RUN_ID}/E10/evidence_pack.json") == evidence_body
        qa_bytes = store.get(f"local:{RUN_ID}/Q10/qa_manifest.json")
        manifest_bytes = store.get(f"local:{RUN_ID}/Q10/manifest.json")
        metrics_bytes = store.get(f"local:{RUN_ID}/Q10/metrics.json")
        qa = _object(json.loads(qa_bytes))
        manifest = _object(json.loads(manifest_bytes))
        metrics = _object(json.loads(metrics_bytes))
        assert qa["qa_pass"] is True
        assert qa["signature"] == result.document["signature"]
        assert manifest["qa_manifest_sha256"] == hashlib.sha256(qa_bytes).hexdigest()
        assert manifest["evidence_modified"] is False
        assert metrics["qa_pass"] is True
        assert metrics["unknown_license_external_release"] == 0
        database = root / RUN_ID / "Q10" / "qa.sqlite"
        connection = sqlite3.connect(database)
        try:
            row = connection.execute(
                "SELECT qa_pass, signature, release_status FROM qa_runs WHERE run_id = ?",
                (RUN_ID,),
            ).fetchone()
            assert row == (1, result.document["signature"], "PASS")
            stored = connection.execute(
                "SELECT check_name, passed FROM qa_checks WHERE run_id = ? ORDER BY check_name",
                (RUN_ID,),
            ).fetchall()
        finally:
            connection.close()
        assert stored == [
            ("citation_coverage", 1),
            ("format", 1),
            ("numeric_date_consistency", 1),
            ("pii", 1),
            ("unknown_license_external_release", 1),
            ("url_traceability", 1),
        ]


def test_cli_strict_exit_codes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_fixture(root, fail=False)
        manifest = root / "report_manifest.json"
        assert main(["--manifest", str(manifest), "--strict"]) == 0
        _write_fixture(root, fail=True)
        assert main(["--manifest", str(manifest), "--strict"]) == 1
        assert main(["--manifest", str(root / "missing.json"), "--strict"]) == 2


def _review(
    report: dict[str, object],
    pack: dict[str, object],
    markdown: str,
    features: list[dict[str, object]],
    raw_records: list[dict[str, object]],
    snapshots: dict[str, bytes],
    *,
    attempt: int = 1,
) -> QaResult:
    return review_report(
        report,
        pack,
        markdown=markdown,
        features=features,
        raw_records=raw_records,
        snapshots=snapshots,
        run_id=RUN_ID,
        attempt=attempt,
    )


def _pass_parts() -> tuple[
    dict[str, object],
    dict[str, object],
    str,
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, bytes],
]:
    evidence = _evidence()
    text = _sentence(4, 5)
    claim = _claim_row(text, 4, 5)
    report: dict[str, object] = {
        "schema_version": "1.0",
        "ticket": "R10",
        "run_id": RUN_ID,
        "report_id": "report-q10-unit-weekly",
        "report_kind": "weekly",
        "status": "success",
        "claims": [claim],
        "links": [evidence],
    }
    pack: dict[str, object] = {
        "schema_version": "1.0",
        "ticket": "E10",
        "trends": [{"trend_id": TREND, "topic": TOPIC}],
        "claims": [
            {
                "claim_id": SOURCE_CLAIM,
                "trend_id": TREND,
                "template": "growth_7d",
                "status": "supported",
                "window_start": START,
                "window_end": END,
                "inputs": {"growth_previous": 4, "growth_current": 5},
                "supporting_evidence_ids": [EVIDENCE_ID],
            }
        ],
        "evidence": [evidence],
    }
    feature: dict[str, object] = {
        "topic_id": TOPIC,
        "as_of": "2026-10-28T15:00:00Z",
        "window_7d_start": START,
        "window_7d_end": END,
        "growth_previous": 4,
        "growth_current": 5,
    }
    raw: dict[str, object] = {
        "source_id": "openalex",
        "source_record_id": "paper",
        "canonical_url": URL,
        "content_sha256": DIGEST,
        "published_at": PUBLISHED,
        "collected_at": COLLECTED,
        "license_status": "metadata_only",
        "raw_uri": RAW_URI,
    }
    snapshot = json.dumps(
        {"canonical_url": URL, "content_sha256": DIGEST},
        separators=(",", ":"),
    ).encode("utf-8")
    markdown = text + "\n" + URL + "\n"
    return report, pack, markdown, [feature], [raw], {RAW_URI: snapshot}


def _numbered_claims(
    count: int, *, bad: int
) -> tuple[
    dict[str, object],
    dict[str, object],
    str,
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, bytes],
]:
    report, pack, _markdown, features, raw_records, snapshots = _pass_parts()
    claims: list[dict[str, object]] = []
    for index in range(count):
        value = 9 if index < bad else 5
        text = f"主题 {TOPIC} 的独立记录数为 {value}。证据 {EVIDENCE_ID}。"
        claim = _claim_row(text, 4, value)
        claim["claim_id"] = f"rc_{index}"
        claim["numbers"] = [{"name": "growth_current", "value": value}]
        claims.append(claim)
    report["claims"] = claims
    markdown = "\n".join(str(claim["text"]) for claim in claims) + "\n" + URL + "\n"
    return report, pack, markdown, features, raw_records, snapshots


def _cloned_claim(report: dict[str, object], index: int) -> dict[str, object]:
    claim = copy.deepcopy(_claim(report))
    claim["claim_id"] = f"rc_{index}"
    return claim


def _claim_row(text: str, previous: int, current: int) -> dict[str, object]:
    return {
        "claim_id": CLAIM_ID,
        "source_claim_id": SOURCE_CLAIM,
        "trend_id": TREND,
        "section": "changes",
        "kind": "fact",
        "template": "growth_7d",
        "text": text,
        "evidence_ids": [EVIDENCE_ID],
        "numbers": [
            {"name": "growth_previous", "value": previous},
            {"name": "growth_current", "value": current},
        ],
    }


def _sentence(previous: int, current: int) -> str:
    return (
        f"主题 {TOPIC} 在 UTC 7 日窗口内的独立记录数由 {previous} 变为 {current}"
        f"（{START} 至 {END}）。证据 {EVIDENCE_ID}。"
    )


def _r10_markdown(claim_text: str, *, extra: tuple[str, ...] = ()) -> str:
    inserted: list[str] = []
    for line in extra:
        inserted.extend([line, ""])
    body = [
        "# TechPulse 周报草稿",
        "",
        "- 模板版本：template-v1",
        "- 提示词/模型版本：template-v1/none",
        "- 状态：success",
        "- report_id：report-q10-unit-weekly",
        "- run_id：q10-unit",
        "",
        "## 趋势",
        "",
        "无。",
        "",
        "## 变化",
        "",
        claim_text,
        "",
        (
            "hypothesis：将主题 topic:rag 的独立记录数由 4 变为 999 "
            "解释为增加。该句是推断，不是事实。证据 ev_support。"
        ),
        "",
        *inserted,
        "## 反证",
        "",
        "无。",
        "",
        "## 未知项",
        "",
        "未知：数字 12345 与 2026-10-28T00:00:00Z 只出现在未知项。",
        "",
        "## 方法",
        "",
        "本草稿由确定性模板 template-v1 生成。方法区的数字 9999 不参与事实校验。",
        "",
        "## 链接目录",
        "",
        f"{EVIDENCE_ID} | {URL} | {DIGEST} | {PUBLISHED} | metadata_only | A",
        "",
    ]
    return "\n".join(body)


def _evidence() -> dict[str, object]:
    excerpt = "RAG records changed from 4 to 5."
    return {
        "evidence_id": EVIDENCE_ID,
        "canonical_id": "openalex:paper",
        "source_family": "academic",
        "source_url": URL,
        "source_published_at": PUBLISHED,
        "collected_at": COLLECTED,
        "normalized_excerpt": excerpt,
        "quote_length": len(excerpt),
        "license_status": "metadata_only",
        "quality_tier": "A",
        "sha256": DIGEST,
        "source_id": "openalex",
        "source_record_id": "paper",
    }


def _write_fixture(root: Path, *, fail: bool) -> None:
    report, pack, markdown, features, raw_records, _snapshots = _pass_parts()
    if fail:
        _claim(report)["evidence_ids"] = []
    raw_records[0].pop("raw_uri")
    (root / "draft.md").write_text(markdown, encoding="utf-8")
    (root / "report_claims.json").write_text(_text_json(report), encoding="utf-8")
    (root / "evidence_pack.json").write_text(_text_json(pack), encoding="utf-8")
    (root / "features.jsonl").write_text(_text_json(features[0]) + "\n", encoding="utf-8")
    (root / "raw.jsonl").write_text(_text_json(raw_records[0]) + "\n", encoding="utf-8")
    artifacts = root / "artifacts"
    artifacts.mkdir(exist_ok=True)
    manifest = {
        "run_id": RUN_ID,
        "artifacts_dir": "artifacts",
        "report_claims": "report_claims.json",
        "draft": "draft.md",
        "evidence_pack": "evidence_pack.json",
        "features": "features.jsonl",
        "raw_records": "raw.jsonl",
    }
    (root / "report_manifest.json").write_text(_text_json(manifest), encoding="utf-8")


def _claim(report: dict[str, object]) -> dict[str, object]:
    claims = report["claims"]
    assert isinstance(claims, list)
    return cast(dict[str, object], claims[0])


def _evidence_row(pack: dict[str, object]) -> dict[str, object]:
    evidence = pack["evidence"]
    assert isinstance(evidence, list)
    return cast(dict[str, object], evidence[0])


def _as_int(value: object) -> int:
    assert isinstance(value, int)
    return value


def _checks(document: object) -> dict[str, dict[str, object]]:
    assert isinstance(document, dict)
    checks = document["checks"]
    assert isinstance(checks, dict)
    found: dict[str, dict[str, object]] = {}
    for key, value in checks.items():
        assert isinstance(value, dict)
        found[str(key)] = {str(name): item for name, item in value.items()}
    return found


def _blocker(document: object, code: str) -> dict[str, object]:
    assert isinstance(document, dict)
    blockers = document["blockers"]
    assert isinstance(blockers, list)
    for item in blockers:
        assert isinstance(item, dict)
        if item.get("code") == code:
            return {str(key): value for key, value in item.items()}
    raise AssertionError(code)


def _frozen(
    report: dict[str, object],
    pack: dict[str, object],
    features: list[dict[str, object]],
    raw_records: list[dict[str, object]],
) -> str:
    return json.dumps(
        {"report": report, "pack": pack, "features": features, "raw": raw_records},
        ensure_ascii=False,
        sort_keys=True,
    )


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _text_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
