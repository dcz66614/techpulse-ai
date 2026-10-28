"""R20 topic dossiers, chart series, and evidence cards."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
import tempfile
from pathlib import Path

import pytest

from techpulse.reports import render as render_module
from techpulse.reports.render import render_topics, run_render, source_fingerprint

SUPPORT_URL = "https://example.org/support"
CONTRARY_URL = "https://example.org/contrary"
META_URL = "https://example.org/meta"
UNSPANNED_URL = "https://example.org/unspanned"
LONG_URL = "https://example.org/long"
REPRINT_URL = "https://example.org/reprint-only"
EVIL_URL = "https://evil.example/override"
QA_EVIL_URL = "https://evil.example/qa"
TITLE_SECRET = "TITLE-SECRET-SHOULD-NOT-LEAK"
META_SECRET = "SECRET-FULLTEXT-" + ("甲" * 40)
UNSPANNED_SECRET = "UNSPANNED-SECRET-全文"
LONG_SECRET = "LONGQUOTE-SECRET" * 20
SUPPORT_EXCERPT = "independent records moved"
CONTRARY_EXCERPT = "contrary note"
SPECIAL_TOPIC = "检索「RAG」*增长*"
SPECIAL_EXCERPT = "RAG *grew* 「稳定」"
ENTITY_NAME = "Acme & Co. <研发「甲」>*"


def test_dossier_charts_cards_timeline_and_urls() -> None:
    pack, review, trends, draft, entities = _success_inputs()
    result = render_topics(
        pack,
        review,
        trends,
        draft=draft,
        entities=entities,
        run_id="r20-unit",
    )
    again = render_topics(
        pack,
        review,
        trends,
        draft=draft,
        entities=entities,
        run_id="r20-unit",
    )
    assert result.status == "success"
    assert again.cards == result.cards
    assert again.markdown_files == result.markdown_files
    assert result.metrics["llm_calls"] == 0
    assert result.metrics["paid_api_calls"] == 0
    assert result.metrics["third_party_writes"] == 0
    assert result.metrics["generated_by_ai"] is False
    fingerprint = source_fingerprint(pack, review, trends, draft, entities)
    assert result.cards["source_version"] == fingerprint["source_version"]
    assert result.cards["source_hashes"] == {
        name: fingerprint[name]
        for name in ("contradictions", "draft", "entities", "evidence_pack", "trends")
    }
    topic = _topic(result.cards, "rag")
    markdown = result.markdown_files["topic_rag.md"]
    assert topic["source_version"] == fingerprint["source_version"]
    assert fingerprint["source_version"] in markdown
    assert "由 41 变为 43" in str(topic["summary"])
    assert "由 41 变为 43" in markdown
    assert "不能据此认为不存在反证" in markdown
    assert "SECRET-FACT" not in markdown
    assert "SECRET-FACT" not in json.dumps(result.cards, ensure_ascii=False)
    chart = _object(topic["charts"])
    week = _object(chart["7d"])
    month = _object(chart["28d"])
    assert week["stated_as_fact"] is True
    assert week["display"] == "facts"
    assert [(point["date"], point["name"], point["value"]) for point in _rows(week["points"])] == [
        ("2026-10-22", "growth_previous", 41),
        ("2026-10-28", "growth_current", 43),
    ]
    assert [(point["date"], point["active"]) for point in _rows(month["points"])] == [
        ("2026-10-02", True),
        ("2026-10-28", True),
    ]
    assert month["display"] == "active_dates"
    assert "2026-10-03" not in markdown
    assert "growth_previous=41" in markdown
    assert "growth_current=43" in markdown
    assert "0.5" not in markdown
    support = {str(card["evidence_id"]): card for card in _rows(topic["support_cards"])}
    assert support["ev_support"]["source_url"] == SUPPORT_URL
    assert support["ev_support"]["excerpt"] == SUPPORT_EXCERPT
    assert support["ev_support"]["stated_as_fact"] is True
    assert support["ev_meta"]["source_url"] == META_URL
    assert support["ev_meta"]["excerpt"] is None
    assert support["ev_meta"]["excerpt_reason"] == "metadata_only"
    assert support["ev_unspanned"]["excerpt_reason"] == "span_missing"
    assert support["ev_long"]["excerpt_reason"] == "excerpt_too_long"
    contrary = _rows(topic["contrary_cards"])
    contrary_urls = {
        str(card["source_url"])
        for card in contrary
        if card["evidence_id"] == "ev_contrary" and card["role"] == "contrary"
    }
    assert contrary_urls == {CONTRARY_URL}
    reprint = next(card for card in contrary if card["role"] == "reprint")
    assert reprint["source_url"] is None
    assert reprint["label"] == "reprint"
    assert "observed" not in reprint
    for card in contrary:
        assert "observed" not in card
    timeline = _rows(topic["timeline"])
    assert [(event["evidence_id"], event["role"]) for event in timeline] == [
        ("ev_long", "support"),
        ("ev_support", "support"),
        ("ev_meta", "support"),
        ("ev_unspanned", "support"),
        ("ev_contrary", "contrary"),
        ("ev_contrary", "qa"),
    ]
    assert _urls(markdown) == {SUPPORT_URL, CONTRARY_URL, META_URL, UNSPANNED_URL, LONG_URL}
    rendered = json.dumps(result.cards, ensure_ascii=False)
    for secret in (TITLE_SECRET, META_SECRET, UNSPANNED_SECRET, LONG_SECRET, EVIL_URL, QA_EVIL_URL):
        assert secret not in markdown
        assert secret not in rendered
    assert REPRINT_URL not in markdown
    assert REPRINT_URL not in rendered
    assert "`ev_support`" in markdown
    assert "同源转载，不作为独立反证" in markdown
    assert "不作为事实陈述" in markdown
    company = next(row for row in _rows(topic["entities"]) if row["canonical_id"] == "company:acme")
    assert company["label"] == "unresolved"
    assert company["display_name"] == ENTITY_NAME
    assert "未解析实体不作为已确认身份" in markdown
    assert "Acme &amp; Co. &lt;研发「甲」&gt;\\*" in markdown
    assert ENTITY_NAME not in markdown
    assert ENTITY_NAME in rendered
    assert "研发「甲」" in markdown
    assert "<" not in markdown
    assert "| ---" not in markdown
    assert "<table" not in markdown.lower()
    assert not any(line.startswith("|") for line in markdown.splitlines())
    assert "文本清单" not in result.markdown_files["topic_index.md"]
    changed = copy.deepcopy(pack)
    evidence = changed["evidence"]
    assert isinstance(evidence, list)
    for item in evidence:
        assert isinstance(item, dict)
        if item.get("evidence_id") == "ev_support":
            item["source_url"] = "https://example.org/support-2"
    moved = render_topics(
        changed, review, trends, draft=draft, entities=entities, run_id="r20-unit"
    )
    assert moved.cards["source_version"] != result.cards["source_version"]
    moved_topic = _topic(moved.cards, "rag")
    moved_support = {str(card["evidence_id"]): card for card in _rows(moved_topic["support_cards"])}
    assert moved_support["ev_support"]["source_url"] == "https://example.org/support-2"


def test_window_counts_are_not_expanded_and_disagreements_are_labelled() -> None:
    pack = _empty_pack()
    pack["trends"] = [_pack_trend("trend:counts:7:2026-10-28", "counts")]
    pack["claims"] = [
        _claim(
            "cl_counts",
            "trend:counts:7:2026-10-28",
            ["ev_counts"],
            {"growth_previous": 2, "growth_current": 6},
        )
    ]
    pack["evidence"] = [
        _evidence(
            "ev_counts",
            "https://example.org/counts",
            "counts note",
            "2026-10-22T00:00:00Z",
        )
    ]
    trends = [
        {
            "trend_id": "trend:counts:7:2026-10-28",
            "topic": "counts",
            "window_days": 7,
            "growth_previous": 2,
            "growth_current": 6,
            "window_7d_previous_start": "2026-10-15T00:00:00Z",
            "window_7d_start": "2026-10-22T00:00:00Z",
        }
    ]
    draft = _draft(
        [
            _fact(
                "cl_counts",
                "trend:counts:7:2026-10-28",
                "主题 counts 的独立记录数由 2 变为 6。证据 ev_counts。",
                ["ev_counts"],
                [("growth_previous", 2), ("growth_current", 6)],
            )
        ]
    )
    result = render_topics(pack, _empty_review(), trends, draft=draft, run_id="r20-counts")
    topic = _topic(result.cards, "counts")
    chart = _object(_object(topic["charts"])["7d"])
    assert chart["display"] == "facts"
    assert [(point["date"], point["value"]) for point in _rows(chart["points"])] == [
        ("2026-10-15", 2),
        ("2026-10-22", 6),
    ]
    markdown = result.markdown_files["topic_counts.md"]
    chart_text = markdown.split("### 7 日", 1)[1].split("### 28 日", 1)[0]
    assert "2026-10-16" not in chart_text
    assert "=0" not in chart_text
    clash_pack = _empty_pack()
    clash_pack["trends"] = [_pack_trend("trend:clash:7:2026-10-28", "clash")]
    clash_pack["claims"] = [
        _claim(
            "cl_clash",
            "trend:clash:7:2026-10-28",
            ["ev_clash"],
            {"growth_current": 8},
        )
    ]
    clash_pack["evidence"] = [
        _evidence("ev_clash", "https://example.org/clash", "clash note", "2026-10-22T00:00:00Z")
    ]
    clash_trends = [
        {
            "trend_id": "trend:clash:7:2026-10-28",
            "topic": "clash",
            "window_days": 7,
            "growth_current": 6,
            "window_7d_start": "2026-10-22T00:00:00Z",
        }
    ]
    clash_draft = _draft(
        [
            _fact(
                "cl_clash",
                "trend:clash:7:2026-10-28",
                "主题 clash 有已存储的变化。证据 ev_clash。",
                ["ev_clash"],
                [("growth_current", 6)],
            )
        ]
    )
    clash = render_topics(
        clash_pack,
        _empty_review(),
        clash_trends,
        draft=clash_draft,
        run_id="r20-clash",
    )
    clash_topic = _topic(clash.cards, "clash")
    clash_chart = _object(_object(clash_topic["charts"])["7d"])
    assert clash_chart["status"] == "numeric_disagreement"
    assert clash_chart["stated_as_fact"] is False
    assert clash_chart["display"] == "withheld"
    assert _rows(clash_chart["disputed"])[0]["values"] == [6, 8]
    clash_md = clash.markdown_files["topic_clash.md"]
    clash_section = clash_md.split("### 7 日", 1)[1].split("### 28 日", 1)[0]
    assert "numeric_disagreement" in clash_section
    assert "不作为事实陈述" in clash_section
    assert "growth_current=6" not in clash_section
    assert "growth_current=8" not in clash_section


def test_insufficient_and_contradicted_items_are_labelled() -> None:
    thin_pack = _empty_pack()
    thin_pack["trends"] = [
        _pack_trend("trend:thin:7:2026-10-28", "thin", status="insufficient_evidence")
    ]
    thin_pack["claims"] = [
        {
            "claim_id": "cl_thin",
            "trend_id": "trend:thin:7:2026-10-28",
            "template": "growth_7d",
            "status": "insufficient_evidence",
            "inputs": {"growth_current": 57},
            "supporting_evidence_ids": [],
        }
    ]
    thin_trends = [
        {
            "trend_id": "trend:thin:7:2026-10-28",
            "topic": "thin",
            "window_days": 7,
            "series_7d": [{"date": "2026-10-28", "name": "growth_current", "value": 57}],
        }
    ]
    thin_draft = {
        "schema_version": "1.0",
        "status": "success",
        "claims": [],
        "omissions": [
            {
                "claim_id": "cl_thin",
                "trend_id": "trend:thin:7:2026-10-28",
                "template": "growth_7d",
                "reason": "insufficient_evidence",
            }
        ],
    }
    thin = render_topics(
        thin_pack, _empty_review(), thin_trends, draft=thin_draft, run_id="r20-thin"
    )
    thin_topic = _topic(thin.cards, "thin")
    assert thin_topic["status"] == "insufficient_evidence"
    thin_chart = _object(_object(thin_topic["charts"])["7d"])
    assert thin_chart["stated_as_fact"] is False
    assert _rows(thin_chart["points"])[0]["value"] == 57
    thin_md = thin.markdown_files["topic_thin.md"]
    chart_text = thin_md.split("### 7 日", 1)[1].split("### 28 日", 1)[0]
    assert "insufficient_evidence" in str(thin_topic["summary"])
    assert "insufficient_evidence" in chart_text
    assert "57" not in str(thin_topic["summary"])
    assert "57" not in chart_text
    blocked_pack = _empty_pack()
    blocked_pack["trends"] = [_pack_trend("trend:block:7:2026-10-28", "block")]
    blocked_pack["claims"] = [
        _claim(
            "cl_block",
            "trend:block:7:2026-10-28",
            ["ev_block"],
            {"growth_current": 61},
        )
    ]
    blocked_pack["evidence"] = [
        _evidence("ev_block", "https://example.org/block", "block note", "2026-10-26T00:00:00Z")
    ]
    review = _empty_review()
    review["contradictions"] = [
        {
            "trend_id": "trend:block:7:2026-10-28",
            "claim_id": "cl_block",
            "evidence_id": "ev_block",
            "independent": True,
            "source_url": EVIL_URL,
        }
    ]
    review["chains"] = [
        {
            "chain_id": "cc_block",
            "trend_id": "trend:block:7:2026-10-28",
            "claim_id": "cl_block",
            "status": "conflict",
        }
    ]
    blocked_trends = [
        {
            "trend_id": "trend:block:7:2026-10-28",
            "topic": "block",
            "window_days": 7,
            "series_7d": [{"date": "2026-10-28", "name": "growth_current", "value": 61}],
        }
    ]
    blocked_draft = _draft(
        [
            _fact(
                "cl_block",
                "trend:block:7:2026-10-28",
                "由 61 变为 70。",
                ["ev_block"],
                [("growth_current", 61), ("growth_previous", 70)],
            )
        ]
    )
    blocked = render_topics(
        blocked_pack,
        review,
        blocked_trends,
        draft=blocked_draft,
        run_id="r20-block",
    )
    blocked_topic = _topic(blocked.cards, "block")
    assert blocked_topic["status"] == "contradicted"
    blocked_chart = _object(_object(blocked_topic["charts"])["7d"])
    assert blocked_chart["stated_as_fact"] is False
    assert _rows(blocked_chart["points"])[0]["value"] == 61
    blocked_md = blocked.markdown_files["topic_block.md"]
    assert "由 61 变为 70" not in str(blocked_topic["summary"])
    assert "由 61 变为 70" not in blocked_md
    section = blocked_md.split("### 7 日", 1)[1].split("### 28 日", 1)[0]
    assert "contradicted" in section
    assert "61" not in section
    assert "70" not in str(blocked_topic["summary"])
    assert EVIL_URL not in blocked_md
    card = _rows(blocked_topic["contrary_cards"])[0]
    assert card["source_url"] == "https://example.org/block"
    assert card["stated_as_fact"] is False


def test_special_characters_are_escaped_and_foreign_urls_are_withheld() -> None:
    excerpt_pack_url = "https://example.org/special"
    pack = _empty_pack()
    pack["trends"] = [_pack_trend("trend:special:7:2026-10-28", SPECIAL_TOPIC)]
    pack["claims"] = [
        _claim(
            "cl_special",
            "trend:special:7:2026-10-28",
            ["ev_special", "ev_foreign"],
            {"growth_current": 4},
        )
    ]
    foreign = "see https://evil.example/quote for more"
    pack["evidence"] = [
        _evidence("ev_special", excerpt_pack_url, SPECIAL_EXCERPT, "2026-10-22T00:00:00Z"),
        _evidence("ev_foreign", "https://example.org/foreign", foreign, "2026-10-23T00:00:00Z"),
    ]
    trends = [
        {
            "trend_id": "trend:special:7:2026-10-28",
            "topic": SPECIAL_TOPIC,
            "window_days": 7,
            "series_7d": [{"date": "2026-10-28", "name": "growth_current", "value": 4}],
        }
    ]
    prose = (
        "注意 * _ [x](https://evil.example/a) | <script>alert(1)</script> 中文「引号」\n## 已发布"
    )
    draft = _draft(
        [
            _fact(
                "cl_special",
                "trend:special:7:2026-10-28",
                prose,
                ["ev_special"],
                [("growth_current", 4)],
            )
        ]
    )
    entities = [
        {
            "canonical_id": "topic:special",
            "entity_type": "topic",
            "display_name": SPECIAL_TOPIC,
            "normalized_key": SPECIAL_TOPIC,
            "resolution_status": "unresolved",
        }
    ]
    result = render_topics(
        pack,
        _empty_review(),
        trends,
        draft=draft,
        entities=entities,
        run_id="r20-special",
    )
    topic = _topic(result.cards, SPECIAL_TOPIC)
    markdown = next(
        text for name, text in result.markdown_files.items() if name != "topic_index.md"
    )
    summary = str(topic["summary"])
    assert "中文「引号」" in summary
    assert "https://evil.example" not in summary
    assert "［链接已省略］" in summary
    assert "<script>" in summary
    assert "中文「引号」" in markdown
    assert r"\*" in markdown
    assert r"\_" in markdown
    assert r"\[" in markdown
    assert "&lt;script&gt;" in markdown
    assert "<script>" not in markdown
    assert "## 已发布" not in markdown
    assert "https://evil.example" not in markdown
    assert SPECIAL_EXCERPT in json.dumps(topic["support_cards"], ensure_ascii=False)
    assert r"\*grew\*" in markdown
    assert "「稳定」" in markdown
    foreign_card = next(
        card for card in _rows(topic["support_cards"]) if card["evidence_id"] == "ev_foreign"
    )
    assert foreign_card["excerpt"] is None
    assert foreign_card["excerpt_reason"] == "foreign_url"
    rendered = json.dumps(result.cards, ensure_ascii=False)
    assert "https://evil.example" not in rendered
    assert "\\u4e2d" not in rendered
    assert "<" not in markdown
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        written = run_render(
            pack,
            _empty_review(),
            trends,
            draft=draft,
            entities=entities,
            run_id="r20-special",
            artifacts_dir=root / "artifacts",
            reports_dir=root / "reports",
        )
        cards_path = root / "artifacts" / "r20-special" / "R20" / "cards.json"
        raw = cards_path.read_text(encoding="utf-8")
        assert "中文「引号」" in raw
        assert "\\u4e2d" not in raw
        assert written.cards["source_version"] == result.cards["source_version"]
        dossier = next(
            path
            for path in (root / "reports" / "r20-special").glob("topic_*.md")
            if path.name != "topic_index.md"
        )
        assert "中文「引号」" in dossier.read_text(encoding="utf-8")


def test_failed_topic_is_isolated_and_the_index_degrades() -> None:
    pack = _empty_pack()
    pack["trends"] = [_pack_trend("trend:kept:7:2026-10-28", "kept")]
    pack["claims"] = [
        _claim("cl_kept", "trend:kept:7:2026-10-28", ["ev_kept"], {"growth_current": 11})
    ]
    pack["evidence"] = [
        _evidence("ev_kept", "https://example.org/kept", "kept note", "2026-10-22T00:00:00Z")
    ]
    trends: list[dict[str, object]] = [
        {
            "trend_id": "trend:kept:7:2026-10-28",
            "topic": "kept",
            "window_days": 7,
            "series_7d": [{"date": "2026-10-28", "name": "growth_current", "value": 11}],
        },
        {
            "trend_id": "trend:bad:7:2026-10-28",
            "topic": "bad",
            "window_days": 7,
            "series_7d": [{"date": "2026-10-28", "value": float("nan")}],
        },
        {
            "trend_id": "trend:broken:7:2026-10-28",
            "topic": "broken",
            "window_days": 7,
            "series_7d": "nope",
        },
    ]
    draft = _draft(
        [
            _fact(
                "cl_kept",
                "trend:kept:7:2026-10-28",
                "主题 kept 有记录。证据 ev_kept。",
                ["ev_kept"],
                [("growth_current", 11)],
            )
        ]
    )
    result = render_topics(pack, _empty_review(), trends, draft=draft, run_id="r20-isolate")
    assert result.status == "degraded"
    assert _topic(result.cards, "kept")["status"] == "success"
    assert _topic(result.cards, "bad")["status"] == "isolated"
    assert _topic(result.cards, "bad")["defect"] == "non_finite_series"
    assert _topic(result.cards, "bad")["support_cards"] == []
    assert _topic(result.cards, "broken")["defect"] == "series_invalid"
    kept = result.markdown_files["topic_kept.md"]
    bad = result.markdown_files["topic_bad.md"]
    assert "https://example.org/kept" in kept
    assert "https://example.org/kept" not in bad
    assert "NaN" not in bad
    assert "non_finite_series" in bad
    index = result.markdown_files["topic_index.md"]
    assert "周报降级为文本清单" in index
    assert "non_finite_series" in index
    assert "series_invalid" in index
    encoded = json.dumps(result.cards, allow_nan=False)
    assert "NaN" not in encoded
    source_version = result.cards["source_version"]
    assert isinstance(source_version, str)
    assert str(source_version) in kept
    assert str(source_version) in bad
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run_render(
            pack,
            _empty_review(),
            trends,
            draft=draft,
            run_id="r20-isolate",
            artifacts_dir=root / "artifacts",
            reports_dir=root / "reports",
        )
        stored = json.loads(
            (root / "artifacts" / "r20-isolate" / "R20" / "cards.json").read_text(encoding="utf-8")
        )
        assert stored["status"] == "degraded"
        assert "NaN" not in json.dumps(stored)


def test_schema_error_invalid_run_id_and_no_network() -> None:
    failed = render_topics(
        {"schema_version": "9.0"},
        _empty_review(),
        [],
        run_id="r20-schema",
    )
    assert failed.status == "schema_error"
    assert failed.cards["topics"] == []
    assert failed.metrics["llm_calls"] == 0
    assert "未生成专题卡片" in failed.markdown_files["topic_index.md"]
    assert list(failed.markdown_files) == ["topic_index.md"]
    empty = render_topics(_empty_pack(), _empty_review(), [], run_id="r20-empty")
    assert empty.status == "success"
    assert empty.cards["topics"] == []
    assert "无主题" in empty.markdown_files["topic_index.md"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with pytest.raises(ValueError, match="invalid run_id"):
            run_render(
                _empty_pack(),
                _empty_review(),
                [],
                run_id="../escape",
                artifacts_dir=root / "artifacts",
                reports_dir=root / "reports",
            )
        assert not any((root / "reports").rglob("*.md"))
    source = inspect.getsource(render_module)
    for banned in ("httpx", "requests", "urllib", "socket", "openai", "getenv", "environ"):
        assert banned not in source


def test_run_render_writes_matching_artifacts() -> None:
    pack, review, trends, draft, entities = _success_inputs()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = run_render(
            pack,
            review,
            trends,
            draft=draft,
            entities=entities,
            run_id="r20-files",
            artifacts_dir=root / "artifacts",
            reports_dir=root / "reports",
        )
        folder = root / "artifacts" / "r20-files" / "R20"
        cards_bytes = (folder / "cards.json").read_bytes()
        cards = json.loads(cards_bytes)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
        assert cards["source_version"] == result.cards["source_version"]
        assert manifest["cards_sha256"] == hashlib.sha256(cards_bytes).hexdigest()
        assert manifest["source_version"] == cards["source_version"]
        assert metrics["llm_calls"] == 0
        assert metrics["paid_api_calls"] == 0
        assert metrics["status"] == "success"
        dossier = (root / "reports" / "r20-files" / "topic_rag.md").read_text(encoding="utf-8")
        index = (root / "reports" / "r20-files" / "topic_index.md").read_text(encoding="utf-8")
        assert cards["source_version"] in dossier
        assert cards["source_version"] in index
        assert SUPPORT_URL in dossier
        listed = next(row for row in manifest["topics"] if row["markdown_file"] == "topic_rag.md")
        assert listed["markdown_sha256"] == hashlib.sha256(dossier.encode("utf-8")).hexdigest()
        assert _urls(dossier) == {SUPPORT_URL, CONTRARY_URL, META_URL, UNSPANNED_URL, LONG_URL}


def _success_inputs() -> tuple[
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
    dict[str, object],
    list[dict[str, object]],
]:
    pack = _empty_pack()
    pack["trends"] = [
        _pack_trend("trend:rag:7:2026-10-28", "rag"),
        _pack_trend("trend:rag:28:2026-10-28", "rag"),
    ]
    support_ids = ["ev_long", "ev_meta", "ev_support", "ev_unspanned"]
    pack["claims"] = [
        _claim(
            "cl_growth",
            "trend:rag:7:2026-10-28",
            support_ids,
            {"growth_previous": 41, "growth_current": 43},
        ),
        _claim(
            "cl_conflict",
            "trend:rag:7:2026-10-28",
            ["ev_contrary"],
            {"growth_previous": 99, "growth_current": 99},
        ),
    ]
    pack["evidence"] = [
        _evidence("ev_long", LONG_URL, LONG_SECRET, "2026-10-21T00:00:00Z"),
        _evidence(
            "ev_meta",
            META_URL,
            META_SECRET,
            "2026-10-23T00:00:00Z",
            license_status="metadata_only",
        ),
        _evidence("ev_support", SUPPORT_URL, SUPPORT_EXCERPT, "2026-10-22T00:00:00Z"),
        _evidence(
            "ev_unspanned",
            UNSPANNED_URL,
            UNSPANNED_SECRET,
            "2026-10-24T00:00:00Z",
            span=False,
        ),
        _evidence("ev_contrary", CONTRARY_URL, CONTRARY_EXCERPT, "2026-10-26T00:00:00Z"),
    ]
    review = _empty_review()
    review["candidates"] = [
        {
            "trend_id": "trend:rag:7:2026-10-28",
            "major_conflict": False,
            "downgraded": False,
            "contrary_search_incomplete": False,
        }
    ]
    review["chains"] = [
        {
            "chain_id": "cc_growth",
            "trend_id": "trend:rag:7:2026-10-28",
            "claim_id": "cl_growth",
            "status": "counterevidence_not_found",
        },
        {
            "chain_id": "cc_conflict",
            "trend_id": "trend:rag:7:2026-10-28",
            "claim_id": "cl_conflict",
            "status": "conflict",
        },
    ]
    review["contradictions"] = [
        {
            "trend_id": "trend:rag:7:2026-10-28",
            "claim_id": "cl_conflict",
            "evidence_id": "ev_contrary",
            "independent": True,
            "source_url": EVIL_URL,
        }
    ]
    review["reprints"] = [
        {
            "trend_id": "trend:rag:7:2026-10-28",
            "claim_id": "cl_growth",
            "evidence_id": "ev_reprint",
            "kept_evidence_id": "ev_support",
            "source_url": REPRINT_URL,
        }
    ]
    review["qa_queue"] = [
        {
            "qa_id": "qa_1",
            "trend_id": "trend:rag:7:2026-10-28",
            "claim_id": "cl_qa",
            "evidence_id": "ev_contrary",
            "reason": "numeric_inconsistency",
            "source_url": QA_EVIL_URL,
            "observed": {"growth_current": 12345},
        }
    ]
    trends = [
        {
            "trend_id": "trend:rag:7:2026-10-28",
            "topic": "rag",
            "window_days": 7,
            "score": 0.5,
            "confidence": "medium",
            "series_7d": [
                {"date": "2026-10-28", "name": "growth_current", "value": 43},
                {"date": "2026-10-22", "name": "growth_previous", "value": 41},
            ],
        },
        {
            "trend_id": "trend:rag:28:2026-10-28",
            "topic": "rag",
            "window_days": 28,
            "score": 0.5,
            "confidence": "medium",
            "active_dates_28d": ["2026-10-28", "2026-10-02"],
        },
    ]
    draft = _draft(
        [
            _fact(
                "cl_growth",
                "trend:rag:7:2026-10-28",
                "主题 rag 在 UTC 7 日窗口内的独立记录数由 41 变为 43。证据 ev_support。",
                support_ids,
                [("growth_previous", 41), ("growth_current", 43)],
            ),
            _fact(
                "cl_conflict",
                "trend:rag:7:2026-10-28",
                "SECRET-FACT 由 99 变为 99。",
                ["ev_contrary"],
                [("growth_current", 99)],
            ),
        ]
    )
    draft["links"] = [
        {"evidence_id": "ev_support", "source_url": "https://evil.example/from-draft"}
    ]
    entities: list[dict[str, object]] = [
        {
            "canonical_id": "topic:rag",
            "entity_type": "topic",
            "display_name": "rag",
            "normalized_key": "rag",
            "resolution_status": "resolved",
        },
        {
            "canonical_id": "company:acme",
            "entity_type": "company",
            "display_name": ENTITY_NAME,
            "normalized_key": "acme",
            "resolution_status": "unresolved",
            "aliases": ["rag"],
        },
    ]
    return pack, review, trends, draft, entities


def _empty_pack() -> dict[str, object]:
    return {"schema_version": "1.0", "ticket": "E10", "trends": [], "claims": [], "evidence": []}


def _empty_review() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "ticket": "E20",
        "candidates": [],
        "chains": [],
        "contradictions": [],
        "reprints": [],
        "qa_queue": [],
    }


def _pack_trend(trend_id: str, topic: str, status: str = "supported") -> dict[str, object]:
    return {"trend_id": trend_id, "topic": topic, "status": status}


def _claim(
    claim_id: str,
    trend_id: str,
    evidence_ids: list[str],
    inputs: dict[str, object],
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "trend_id": trend_id,
        "template": "growth_7d",
        "status": "supported",
        "window_start": "2026-10-22T00:00:00Z",
        "window_end": "2026-10-29T00:00:00Z",
        "inputs": inputs,
        "supporting_evidence_ids": evidence_ids,
    }


def _draft(claims: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "ticket": "R10",
        "status": "success",
        "claims": claims,
        "omissions": [],
    }


def _fact(
    source_claim_id: str,
    trend_id: str,
    text: str,
    evidence_ids: list[str],
    numbers: list[tuple[str, int]],
) -> dict[str, object]:
    return {
        "claim_id": "rc_" + source_claim_id,
        "source_claim_id": source_claim_id,
        "trend_id": trend_id,
        "section": "trends",
        "kind": "fact",
        "template": "growth_7d",
        "text": text,
        "evidence_ids": evidence_ids,
        "numbers": [{"name": name, "value": value} for name, value in numbers],
    }


def _evidence(
    evidence_id: str,
    url: str,
    excerpt: str,
    published: str,
    *,
    license_status: str = "reusable_excerpt",
    span: bool = True,
) -> dict[str, object]:
    item: dict[str, object] = {
        "evidence_id": evidence_id,
        "canonical_id": "company:acme",
        "source_family": "academic",
        "source_url": url,
        "source_published_at": published,
        "collected_at": "2026-10-27T00:00:00Z",
        "normalized_excerpt": excerpt,
        "quote_length": len(excerpt),
        "license_status": license_status,
        "quality_tier": "A",
        "sha256": hashlib.sha256(evidence_id.encode("utf-8")).hexdigest(),
        "title": TITLE_SECRET,
    }
    if span:
        item["citation_span"] = {"field": "abstract_or_excerpt", "start": 0, "end": len(excerpt)}
    else:
        item["citation_span_missing"] = True
    return item


def _topic(cards: dict[str, object], name: str) -> dict[str, object]:
    for topic in _rows(cards.get("topics")):
        if topic.get("topic") == name:
            return topic
    raise AssertionError(name)


def _urls(markdown: str) -> set[str]:
    return set(re.findall(r"https://[^\s`<>\\]+", markdown))


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [_object(item) for item in value]


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object")
    return {str(key): item for key, item in value.items()}
