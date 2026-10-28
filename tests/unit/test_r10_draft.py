"""R10 Chinese Markdown drafts from stored evidence."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from techpulse.evidence.contradictions import review_contradictions
from techpulse.evidence.verify import build_evidence_pack
from techpulse.reports import draft as draft_module
from techpulse.reports.draft import draft_report, run_draft

TREND = "trend:topic:rag:7:2026-10-28"
START = "2026-10-22T00:00:00Z"
END = "2026-10-29T00:00:00Z"
AS_OF = "2026-10-28T15:00:00Z"
COLLECTED = "2026-10-27T00:00:00Z"
EXCERPT = "RAG records changed from 4 to 5."
SUPPORT_URL = "https://example.org/openalex/paper"
REPRINT_URL = "https://example.org/reprint-only"


def test_every_claim_maps_to_an_evidence_id() -> None:
    pack, review, trends = _success_inputs()
    draft = draft_report(pack, review, trends, run_id="r10-unit")
    assert draft.status == "success"
    assert draft.document["template_version"] == "template-v1"
    assert draft.document["prompt_model_version"] == "template-v1/none"
    assert draft.document["prompt_version"] == "template-v1"
    assert draft.document["model_version"] == "none"
    assert draft.document["generated_by_ai"] is False
    assert draft.metrics["llm_calls"] == 0
    assert draft.metrics["paid_api_calls"] == 0
    assert draft.metrics["third_party_writes"] == 0
    known = _known_ids(pack, review)
    claims = _rows(draft.document["claims"])
    assert claims
    assert {claim["kind"] for claim in claims} >= {"fact", "hypothesis", "counter"}
    for claim in claims:
        evidence_ids = _strings(claim["evidence_ids"])
        assert evidence_ids
        assert set(evidence_ids) <= known
        for evidence_id in evidence_ids:
            assert evidence_id in str(claim["text"])
        for number in _rows(claim["numbers"]):
            inputs = _inputs_for(pack, str(claim["source_claim_id"]))
            assert number["value"] == inputs[str(number["name"])]
    facts = [claim for claim in claims if claim["kind"] == "fact"]
    growth = next(claim for claim in facts if claim["template"] == "growth_7d")
    assert "由 4 变为 5" in str(growth["text"])
    assert "增加" not in str(growth["text"])
    assert "hypothesis" not in str(growth["text"])
    hypothesis = next(claim for claim in claims if claim["kind"] == "hypothesis")
    assert str(hypothesis["text"]).startswith("hypothesis：")
    assert "增加" in str(hypothesis["text"])
    assert "不是事实" in str(hypothesis["text"])
    diversity = next(claim for claim in facts if claim["template"] == "diversity_7d")
    assert "academic、code、community" in str(diversity["text"])
    reprint = next(claim for claim in claims if claim["template"] == "reprint")
    assert "同源转载" in str(reprint["text"])
    assert "ev_reprint" in str(reprint["text"])
    assert REPRINT_URL not in draft.markdown
    head, directory = draft.markdown.split("## 链接目录", 1)
    assert "https://" not in head
    assert "http://" not in head
    assert SUPPORT_URL in directory
    assert _urls(draft.markdown) == {SUPPORT_URL}
    for title in ("趋势", "变化", "反证", "未知项", "方法", "链接目录"):
        assert f"## {title}" in draft.markdown
    assert "template-v1/none" in draft.markdown
    assert "2.5" not in draft.markdown
    assert "%" not in draft.markdown
    assert "客户" not in draft.markdown
    assert "evil.example" not in draft.markdown
    assert "RAG records changed" not in draft.markdown
    assert "不作为事实陈述" in draft.markdown
    again = draft_report(pack, review, trends, run_id="r10-unit")
    assert again.document == draft.document
    assert again.markdown == draft.markdown


def test_no_material_update_when_evidence_ids_are_unchanged() -> None:
    pack, review, trends = _success_inputs()
    first = draft_report(pack, review, trends, run_id="r10-unit")
    assert first.evidence_ids
    second = draft_report(
        pack,
        review,
        trends,
        previous_evidence_ids=first.evidence_ids,
        run_id="r10-unit",
    )
    assert second.status == "no_material_update"
    assert second.document["claims"] == []
    assert second.document["links"] == []
    assert second.document["prompt_model_version"] == "template-v1/none"
    assert second.metrics["new_evidence_ids"] == 0
    assert second.metrics["fact_claims"] == 0
    assert second.metrics["llm_calls"] == 0
    assert "no_material_update" in second.markdown
    assert "没有新的证据" in second.markdown
    assert "https://" not in second.markdown
    assert "由 4 变为 5" not in second.markdown
    assert "ev_support" not in second.markdown
    opened = draft_report(pack, review, trends, previous_evidence_ids=[], run_id="r10-unit")
    assert opened.status == "success"
    assert opened.document == first.document


def test_missing_evidence_id_is_not_a_material_update() -> None:
    pack, review, trends = _success_inputs()
    baseline = draft_report(pack, review, trends, run_id="r10-missing-id")
    assert "ev_not_in_evidence" not in baseline.evidence_ids
    phantom = {
        "claim_id": "cl_not_in_evidence",
        "trend_id": TREND,
        "template": "growth_7d",
        "status": "supported",
        "window_start": START,
        "window_end": END,
        "text": "https://evil.example/missing from 8 to 9",
        "inputs": {"growth_previous": 8, "growth_current": 9},
        "supporting_evidence_ids": ["ev_not_in_evidence"],
    }
    mixed = {
        "claim_id": "cl_mixed_missing",
        "trend_id": TREND,
        "template": "persistence_28d",
        "status": "supported",
        "window_start": START,
        "window_end": END,
        "inputs": {"persistence_numerator": 3, "persistence_denominator": 28},
        "supporting_evidence_ids": ["ev_support", "ev_not_in_evidence"],
    }
    claims = [*_rows(pack["claims"]), phantom, mixed]
    updated = dict(pack)
    updated["claims"] = claims
    compared = draft_report(
        updated,
        review,
        trends,
        previous_evidence_ids=baseline.evidence_ids,
        run_id="r10-missing-id",
    )
    assert compared.status == "no_material_update"
    assert compared.document["claims"] == []
    assert "ev_not_in_evidence" not in compared.evidence_ids
    assert compared.metrics["new_evidence_ids"] == 0
    assert "由 8 变为 9" not in compared.markdown
    assert "evil.example" not in compared.markdown
    flags = _rows(compared.document["omissions"])
    assert {str(item["claim_id"]) for item in flags} == {
        "cl_not_in_evidence",
        "cl_mixed_missing",
    }
    assert {str(item["reason"]) for item in flags} == {"evidence_not_in_pack"}
    assert "cl_not_in_evidence" in compared.markdown
    assert "不作为事实陈述" in compared.markdown
    opened = draft_report(updated, review, trends, run_id="r10-missing-id")
    assert opened.status == "success"
    assert "ev_not_in_evidence" not in opened.evidence_ids
    rendered = _rows(opened.document["claims"])
    assert all(
        claim["source_claim_id"] not in {"cl_not_in_evidence", "cl_mixed_missing"}
        for claim in rendered
        if claim["kind"] == "fact"
    )
    assert "由 8 变为 9" not in opened.markdown
    assert "28 个 UTC 日中的 3 日" not in opened.markdown
    opened_flags = {
        str(item["claim_id"]): str(item["reason"]) for item in _rows(opened.document["omissions"])
    }
    assert opened_flags["cl_not_in_evidence"] == "evidence_not_in_pack"
    assert opened_flags["cl_mixed_missing"] == "evidence_not_in_pack"
    assert "ev_not_in_evidence" not in opened.markdown


def test_insufficient_evidence_is_not_stated_as_fact() -> None:
    evidence = [_evidence("ev_support", SUPPORT_URL)]
    claim: dict[str, object] = {
        "claim_id": "cl_only",
        "trend_id": TREND,
        "template": "growth_7d",
        "text": "https://evil.example/none changed from 99 to 100",
        "status": "insufficient_evidence",
        "inputs": {"growth_previous": 99, "growth_current": 100},
        "supporting_evidence_ids": ["ev_support"],
    }
    draft = draft_report(_pack([claim], evidence), _review(), _trends(), run_id="r10-empty")
    assert draft.status == "no_material_update"
    assert draft.document["claims"] == []
    assert "99" not in draft.markdown
    assert "100" not in draft.markdown
    assert "evil.example" not in draft.markdown
    assert SUPPORT_URL not in draft.markdown
    assert "https://" not in draft.markdown


def test_missing_inputs_do_not_invent_a_number() -> None:
    claim: dict[str, object] = {
        "claim_id": "cl_blank",
        "trend_id": TREND,
        "template": "growth_7d",
        "status": "supported",
        "inputs": {},
        "supporting_evidence_ids": ["ev_support"],
    }
    draft = draft_report(
        _pack([claim], [_evidence("ev_support", SUPPORT_URL)]),
        _review(),
        _trends(),
        run_id="r10-blank",
    )
    assert draft.status == "no_material_update"
    assert "由 0" not in draft.markdown
    assert SUPPORT_URL not in draft.markdown


def test_contradicted_claim_is_not_a_fact() -> None:
    support = _evidence("ev_support", SUPPORT_URL)
    opposed = _evidence("ev_opposed", "https://example.org/arxiv/opposed", digest="c" * 64)
    contradiction = {
        **opposed,
        "trend_id": TREND,
        "claim_id": "cl_growth",
        "independent": True,
        "major": True,
        "kinds": ["opposing_conclusion"],
        "note": "https://evil.example/note",
    }
    review = _review(
        chains=[
            {
                "chain_id": "cc_growth",
                "trend_id": TREND,
                "claim_id": "cl_growth",
                "status": "conflict",
                "contradiction_ids": ["ev_opposed"],
            }
        ],
        contradictions=[contradiction],
    )
    draft = draft_report(
        _pack([_growth_claim()], [support, opposed]),
        review,
        _trends(),
        run_id="r10-conflict",
    )
    assert draft.status == "success"
    claims = _rows(draft.document["claims"])
    assert claims
    assert all(claim["kind"] != "fact" for claim in claims)
    assert all(claim["kind"] != "hypothesis" for claim in claims)
    counter = next(claim for claim in claims if claim["template"] == "contradiction")
    assert counter["evidence_ids"] == ["ev_opposed"]
    assert "不作为事实陈述" in str(counter["text"])
    assert "opposing_conclusion" in str(counter["text"])
    assert "由 4 变为 5" not in draft.markdown
    assert all(claim["kind"] != "hypothesis" for claim in claims)
    assert "evil.example" not in draft.markdown
    assert "https://example.org/arxiv/opposed" in draft.markdown
    known = _known_ids(_pack([_growth_claim()], [support, opposed]), review)
    for claim in claims:
        assert set(_strings(claim["evidence_ids"])) <= known


def test_numeric_disagreement_is_not_stated_as_fact() -> None:
    observed = {"growth_previous": 4, "growth_current": 999, "url": "https://evil.example/observed"}
    review = _review(
        qa_queue=[
            {
                "qa_id": "qa_numeric",
                "trend_id": TREND,
                "claim_id": "cl_growth",
                "evidence_id": "ev_support",
                "reason": "numeric_inconsistency",
                "observed": observed,
            }
        ]
    )
    draft = draft_report(
        _pack([_growth_claim()], [_evidence("ev_support", SUPPORT_URL)]),
        review,
        _trends(),
        run_id="r10-qa",
    )
    assert draft.status == "success"
    claims = _rows(draft.document["claims"])
    assert all(claim["template"] != "growth_7d" for claim in claims)
    unknown = next(claim for claim in claims if claim["template"] == "qa")
    assert unknown["kind"] == "unknown"
    assert unknown["evidence_ids"] == ["ev_support"]
    assert "不作为事实陈述" in str(unknown["text"])
    assert "由 4 变为 5" not in draft.markdown
    assert "999" not in draft.markdown
    assert "evil.example" not in draft.markdown
    assert set(_strings(unknown["evidence_ids"])) <= _known_ids(
        _pack([_growth_claim()], [_evidence("ev_support", SUPPORT_URL)]),
        review,
    )


def test_schema_error_writes_no_facts() -> None:
    pack = {
        "schema_version": "1.0",
        "trends": [],
        "evidence": [],
        "note": "https://evil.example/schema",
    }
    draft = draft_report(pack, _review(), [], run_id="r10-schema")
    assert draft.status == "schema_error"
    assert draft.document["error"] == "claims"
    assert draft.document["claims"] == []
    assert "结构检查" in draft.markdown
    assert "evil.example" not in draft.markdown
    with pytest.raises(ValueError, match="invalid report_kind"):
        draft_report(pack, _review(), [], report_kind="topic")


def test_run_draft_writes_markdown_artifacts_and_sqlite() -> None:
    pack, review, trends = _success_inputs()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        draft = run_draft(
            pack,
            review,
            trends,
            run_id="r10-unit",
            artifacts_dir=root / "artifacts",
            reports_dir=root / "reports" / "draft",
        )
        markdown_path = root / "reports" / "draft" / "r10-unit" / "weekly.md"
        directory = root / "artifacts" / "r10-unit" / "R10"
        markdown = markdown_path.read_text(encoding="utf-8")
        body = (directory / "report_claims.json").read_bytes()
        manifest = _object(json.loads((directory / "manifest.json").read_text(encoding="utf-8")))
        metrics = _object(json.loads((directory / "metrics.json").read_text(encoding="utf-8")))
        assert markdown == draft.markdown
        assert json.loads(body.decode("utf-8")) == draft.document
        assert manifest["status"] == "success"
        assert manifest["prompt_model_version"] == "template-v1/none"
        assert manifest["template_version"] == "template-v1"
        assert manifest["report_claims_sha256"] == hashlib.sha256(body).hexdigest()
        assert manifest["markdown_sha256"] == hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        assert manifest["content_uri"] == "reports/draft/r10-unit/weekly.md"
        assert metrics["third_party_writes"] == 0
        assert metrics["llm_calls"] == 0
        connection = sqlite3.connect(directory / "reports.sqlite")
        try:
            stored = connection.execute(
                """
                SELECT status, template_version, prompt_model_version, generated_by_ai
                FROM report_drafts
                """
            ).fetchone()
            pairs = connection.execute(
                """
                SELECT claim_id, evidence_id
                FROM report_claim_evidence
                ORDER BY claim_id, evidence_id
                """
            ).fetchall()
        finally:
            connection.close()
        assert stored == ("success", "template-v1", "template-v1/none", 0)
        expected = sorted(
            (str(claim["claim_id"]), evidence_id)
            for claim in _rows(draft.document["claims"])
            for evidence_id in _strings(claim["evidence_ids"])
        )
        assert pairs == expected
        daily = run_draft(
            pack,
            review,
            trends,
            run_id="r10-daily",
            artifacts_dir=root / "artifacts",
            reports_dir=root / "reports" / "draft",
            report_kind="daily",
        )
        daily_path = root / "reports" / "draft" / "r10-daily" / "daily.md"
        assert daily_path.is_file()
        assert daily.markdown.startswith("# TechPulse 日报草稿")
        assert not (root / "reports" / "draft" / "r10-daily" / "weekly.md").exists()
    with pytest.raises(ValueError, match="invalid run_id"):
        run_draft(
            pack,
            review,
            trends,
            run_id="../r10",
            artifacts_dir=Path("."),
            reports_dir=Path("."),
        )


def test_real_e10_pack_and_e20_review_render_without_invented_numbers() -> None:
    records = _support_records()
    pack = build_evidence_pack([_trend(7)], records, features=[_feature()]).document
    review = review_contradictions(pack, records).document
    trends = [_trend(7)]
    draft = draft_report(pack, review, trends, run_id="r10-live")
    assert draft.status == "success"
    assert draft.metrics["llm_calls"] == 0
    assert draft.metrics["paid_api_calls"] == 0
    claims = _rows(draft.document["claims"])
    pack_ids = {str(item["evidence_id"]) for item in _rows(pack["evidence"])}
    facts = [claim for claim in claims if claim["kind"] == "fact"]
    assert facts
    for claim in claims:
        evidence_ids = _strings(claim["evidence_ids"])
        assert evidence_ids
        assert set(evidence_ids) <= pack_ids
        for evidence_id in evidence_ids:
            assert evidence_id in str(claim["text"])
    growth = next(claim for claim in facts if claim["template"] == "growth_7d")
    assert "由 4 变为 5" in str(growth["text"])
    hypothesis = next(claim for claim in claims if claim["kind"] == "hypothesis")
    assert "hypothesis" in str(hypothesis["text"])
    assert "增加" in str(hypothesis["text"])
    for claim in claims:
        source = _claim_by_id(pack, str(claim["source_claim_id"]))
        if source is None:
            assert claim["numbers"] == []
            continue
        inputs = source.get("inputs")
        assert isinstance(inputs, dict)
        for number in _rows(claim["numbers"]):
            assert number["value"] == inputs[str(number["name"])]
    assert "%" not in draft.markdown
    assert "客户" not in draft.markdown
    assert _urls(draft.markdown) <= _urls_in(pack, review)
    head, _directory = draft.markdown.split("## 链接目录", 1)
    assert "https://" not in head
    again = draft_report(
        pack,
        review,
        trends,
        previous_evidence_ids=draft.evidence_ids,
        run_id="r10-live",
    )
    assert again.status == "no_material_update"
    assert again.document["claims"] == []


def test_module_does_not_call_a_model_api() -> None:
    source = inspect.getsource(draft_module)
    folded = source.lower()
    assert "openai" not in folded
    assert "httpx" not in folded
    assert "anthropic" not in folded
    assert "embedder_from_env" not in folded
    assert "os.environ" not in source
    assert "getenv" not in source


def _success_inputs() -> tuple[
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
]:
    evidence = [_evidence("ev_support", SUPPORT_URL)]
    pack = _pack([_growth_claim(), _diversity_claim(), _quality_claim()], evidence)
    review = _review(
        reprints=[
            {
                "trend_id": TREND,
                "claim_id": "cl_growth",
                "evidence_id": "ev_reprint",
                "kept_evidence_id": "ev_support",
                "source_url": REPRINT_URL,
                "sha256": "b" * 64,
            }
        ]
    )
    return pack, review, _trends()


def _growth_claim() -> dict[str, object]:
    return {
        "claim_id": "cl_growth",
        "trend_id": TREND,
        "template": "growth_7d",
        "text": "https://evil.example/from-claim 80% customer case from 4 to 5",
        "status": "supported",
        "window_start": START,
        "window_end": END,
        "inputs": {"growth_previous": 4, "growth_current": 5},
        "supporting_evidence_ids": ["ev_support"],
    }


def _diversity_claim() -> dict[str, object]:
    return {
        "claim_id": "cl_diversity",
        "trend_id": TREND,
        "template": "diversity_7d",
        "status": "supported",
        "window_start": START,
        "window_end": END,
        "inputs": {
            "diversity_numerator": 3,
            "source_families": ["academic", "code", "community"],
        },
        "supporting_evidence_ids": ["ev_support"],
    }


def _quality_claim() -> dict[str, object]:
    return {
        "claim_id": "cl_quality",
        "trend_id": TREND,
        "template": "quality_7d",
        "text": "https://evil.example/quality",
        "status": "insufficient_evidence",
        "inputs": {"quality_numerator": 2.5, "quality_denominator": 3},
        "supporting_evidence_ids": [],
    }


def _pack(
    claims: list[dict[str, object]],
    evidence: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "ticket": "E10",
        "trends": [
            {
                "trend_id": TREND,
                "topic": "topic:rag",
                "window_days": 7,
                "score": 0.5,
                "status": "supported",
            }
        ],
        "claims": claims,
        "evidence": evidence,
    }


def _review(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "1.0",
        "ticket": "E20",
        "candidates": [
            {
                "trend_id": TREND,
                "confidence": "high",
                "contrary_search_incomplete": False,
                "major_conflict": False,
            }
        ],
        "chains": [],
        "contradictions": [],
        "reprints": [],
        "qa_queue": [],
    }
    document.update(overrides)
    return document


def _trends() -> list[dict[str, object]]:
    return [{"trend_id": TREND, "topic": "topic:rag", "window_days": 7, "score": 0.5}]


def _evidence(evidence_id: str, url: str, digest: str | None = None) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "canonical_id": "openalex:paper",
        "source_family": "academic",
        "source_url": url,
        "source_published_at": "2026-10-22T00:00:00Z",
        "collected_at": COLLECTED,
        "normalized_excerpt": EXCERPT,
        "quote_length": len(EXCERPT),
        "license_status": "metadata_only",
        "quality_tier": "A",
        "sha256": digest if digest is not None else "a" * 64,
    }


def _known_ids(pack: Mapping[str, object], review: Mapping[str, object]) -> set[str]:
    found: set[str] = set()
    for item in _rows(pack.get("evidence")):
        found.add(str(item["evidence_id"]))
    for key in ("contradictions", "reprints", "qa_queue"):
        for item in _rows(review.get(key)):
            if isinstance(item.get("evidence_id"), str):
                found.add(str(item["evidence_id"]))
            if isinstance(item.get("kept_evidence_id"), str):
                found.add(str(item["kept_evidence_id"]))
    return found


def _claim_by_id(pack: Mapping[str, object], claim_id: str) -> Mapping[str, object] | None:
    for claim in _rows(pack.get("claims")):
        if claim.get("claim_id") == claim_id:
            return claim
    return None


def _inputs_for(pack: Mapping[str, object], claim_id: str) -> Mapping[str, object]:
    for claim in _rows(pack.get("claims")):
        if claim.get("claim_id") == claim_id:
            inputs = claim.get("inputs")
            if isinstance(inputs, dict):
                return {str(key): value for key, value in inputs.items()}
    raise AssertionError(claim_id)


def _feature() -> dict[str, object]:
    return {
        "topic_id": "topic:rag",
        "as_of": AS_OF,
        "growth_current": 5,
        "growth_previous": 4,
        "growth_unknown": False,
        "window_7d_start": START,
        "window_7d_end": END,
        "diversity_numerator": 3,
        "source_families": ["academic", "code", "community"],
        "quality_numerator": 2.5,
        "quality_denominator": 3,
        "quality_unknown": False,
        "persistence_numerator": 9,
        "persistence_denominator": 28,
        "window_28d_start": "2026-10-01T00:00:00Z",
        "window_28d_end": END,
    }


def _trend(window: int) -> dict[str, object]:
    return {
        "trend_id": f"trend:topic:rag:{window}:2026-10-28",
        "topic_id": "topic:rag",
        "topic": "topic:rag",
        "window_days": window,
        "score": 0.5,
        "confidence": "high",
        "experimental": False,
        "score_kind": "calibrated",
        "contrary_search_executed": True,
        "qa_pass": True,
        "last_verified_at": AS_OF,
    }


def _support_records() -> list[dict[str, object]]:
    return [
        _doc("openalex", "paper", "academic", "A", "2026-10-22T00:00:00Z"),
        _doc("arxiv", "note", "academic", "A", "2026-10-23T00:00:00Z"),
        _doc("github", "repo", "code", "A", "2026-10-24T00:00:00Z"),
        _doc("hn", "thread", "community", "B", "2026-10-25T00:00:00Z"),
    ]


def _doc(source: str, sid: str, family: str, tier: str, published: str) -> dict[str, object]:
    return {
        "source_id": source,
        "source_record_id": sid,
        "source_family": family,
        "quality_tier": tier,
        "canonical_id": f"{source}:{sid}",
        "canonical_url": f"https://example.org/{source}/{sid}",
        "title": f"RAG note {sid}",
        "abstract_or_excerpt": EXCERPT,
        "published_at": published,
        "collected_at": COLLECTED,
        "license_status": "metadata_only",
        "content_sha256": hashlib.sha256(f"{source}:{sid}".encode()).hexdigest(),
        "policy_snapshot_id": "fixture-2026-10-27",
        "citation_span": {"field": "abstract_or_excerpt", "start": 0, "end": len(EXCERPT)},
    }


def _urls(markdown: str) -> set[str]:
    found: set[str] = set()
    for token in markdown.split():
        if token.startswith("https://") or token.startswith("http://"):
            found.add(token.strip(" |"))
        for part in token.split("|"):
            cleaned = part.strip()
            if cleaned.startswith("https://") or cleaned.startswith("http://"):
                found.add(cleaned)
    return found


def _urls_in(pack: Mapping[str, object], review: Mapping[str, object]) -> set[str]:
    found: set[str] = set()
    for item in _rows(pack.get("evidence")):
        url = item.get("source_url")
        if isinstance(url, str):
            found.add(url)
    for key in ("contradictions", "reprints", "qa_queue"):
        for item in _rows(review.get(key)):
            url = item.get("source_url")
            if isinstance(url, str):
                found.add(url)
    return found


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [_object(item) for item in value]


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object")
    return {str(key): item for key, item in value.items()}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [item for item in value if isinstance(item, str)]
