"""R10 daily and weekly Markdown drafts.

The draft is filled from deterministic templates. It does not call a model
or a paid API. A fact is a supported E10 claim whose numbers are copied from
that claim's stored inputs and whose evidence ids already exist on a
schema-valid evidence item. E20 contradictions and QA disagreements are not
stated as facts. An inference is labelled ``hypothesis``.

``template_version`` is ``template-v1``. The prompt/model version is
``template-v1/none``. An evidence id counts only when an evidence item in the
pack carries it. A claim that cites an id absent from the pack is not stated
as a fact and is recorded as an omission. When a previous id list already
contains every counted id, or when nothing can be cited, the status is
``no_material_update``.

Bytes for the JSON artifacts go through ``ObjectStore``. The local backend
writes under the artifacts directory. The Markdown file is the draft under
``reports/draft/{run_id}/``. SQLite stores the claim-to-evidence map.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from techpulse.contracts import EvidenceItem
from techpulse.workflows.activities import LocalObjectStore, ObjectStore

TEMPLATE_VERSION = "template-v1"
PROMPT_MODEL_VERSION = "template-v1/none"
_MODEL_VERSION = "none"
_CHANGE_TEMPLATES = frozenset({"growth_7d"})
_TREND_TEMPLATES = frozenset(
    {
        "persistence_28d",
        "diversity_7d",
        "quality_7d",
        "developer_7d",
        "novelty_7d",
    }
)
_UNKNOWN_TEMPLATES = frozenset({"growth_7d_unknown"})
_FACT_TEMPLATES = _CHANGE_TEMPLATES | _TREND_TEMPLATES | _UNKNOWN_TEMPLATES
_SECTION_ORDER = ("trends", "changes", "counter_evidence", "unknowns")
_SECTION_TITLE = {
    "trends": "趋势",
    "changes": "变化",
    "counter_evidence": "反证",
    "unknowns": "未知项",
}
_KIND_ORDER = {"fact": 0, "hypothesis": 1, "counter": 2, "unknown": 3}
_ID = re.compile(r"^[A-Za-z0-9_:-]{1,120}$")
_LABEL = re.compile(r"^[a-z0-9_]{1,64}$")
_TOPIC = re.compile(r"^[\w:-]{1,200}$")
_FAMILY = re.compile(r"^[a-z_]{1,32}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_EVIDENCE_FIELDS = (
    "evidence_id",
    "canonical_id",
    "source_family",
    "source_url",
    "source_published_at",
    "collected_at",
    "normalized_excerpt",
    "quote_length",
    "license_status",
    "quality_tier",
    "sha256",
)
_METHOD = (
    "本草稿由确定性模板 template-v1 生成。"
    "提示词/模型版本为 template-v1/none。"
    "未调用语言模型，未调用付费 API。"
    "事实句只引用证据 ID。"
    "推断句带 hypothesis 标记。"
    "证据不足或已有冲突的内容不写成事实。"
    "数字取自证据包中已存储的主张输入，保持原有计数或分数。"
    "链接只复制已通过 schema 的 source_url。"
)
_DDL = """
CREATE TABLE IF NOT EXISTS report_drafts (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    report_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    template_version TEXT NOT NULL,
    prompt_model_version TEXT NOT NULL,
    content_uri TEXT NOT NULL,
    claims_uri TEXT NOT NULL,
    generated_by_ai INTEGER NOT NULL CHECK (generated_by_ai IN (0, 1))
);
CREATE TABLE IF NOT EXISTS report_claims (
    claim_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES report_drafts (report_id),
    trend_id TEXT NOT NULL,
    source_claim_id TEXT NOT NULL,
    section TEXT NOT NULL,
    kind TEXT NOT NULL,
    template TEXT NOT NULL,
    claim_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_claim_evidence (
    claim_id TEXT NOT NULL REFERENCES report_claims (claim_id),
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (claim_id, evidence_id)
);
"""


@dataclass(frozen=True)
class ReportDraft:
    status: str
    document: dict[str, object]
    markdown: str
    metrics: dict[str, object]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Claim:
    claim_id: str
    source_claim_id: str
    trend_id: str
    section: str
    kind: str
    template: str
    text: str
    evidence_ids: tuple[str, ...]
    numbers: tuple[tuple[str, int | float], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "source_claim_id": self.source_claim_id,
            "trend_id": self.trend_id,
            "section": self.section,
            "kind": self.kind,
            "template": self.template,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "numbers": [{"name": name, "value": value} for name, value in self.numbers],
        }


def draft_report(
    evidence_pack: Mapping[str, object],
    contradictions: Mapping[str, object],
    trends: Sequence[Mapping[str, object]],
    *,
    previous_evidence_ids: Sequence[str] | None = None,
    run_id: str | None = None,
    report_kind: str = "weekly",
) -> ReportDraft:
    """Fill one Chinese draft from an E10 pack, an E20 review, and T30 candidates."""
    checked_kind = _kind(report_kind)
    error = _structure_error(evidence_pack, contradictions, trends)
    if error is not None:
        return _terminal(
            "schema_error",
            run_id,
            checked_kind,
            (),
            error=error,
            compared=False,
            new_ids=None,
        )
    seen = _raw_ids(evidence_pack, contradictions)
    prior = _prior(previous_evidence_ids)
    new_ids = None if prior is None else tuple(sorted(seen - prior))
    unchanged = prior is not None and not new_ids
    claims, omissions, links = _assemble(evidence_pack, contradictions, trends)
    if unchanged or not claims:
        flagged = [dict(item) for item in omissions if item.get("reason") == "evidence_not_in_pack"]
        return _terminal(
            "no_material_update",
            run_id,
            checked_kind,
            tuple(sorted(seen)),
            error=None,
            compared=prior is not None,
            new_ids=() if unchanged else new_ids,
            omissions=flagged,
        )
    return _publish(
        claims,
        omissions,
        links,
        run_id,
        checked_kind,
        tuple(sorted(seen)),
        compared=prior is not None,
        new_ids=new_ids,
    )


def run_draft(
    evidence_pack: Mapping[str, object],
    contradictions: Mapping[str, object],
    trends: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    reports_dir: Path,
    previous_evidence_ids: Sequence[str] | None = None,
    report_kind: str = "weekly",
    store: ObjectStore | None = None,
    database: Path | None = None,
) -> ReportDraft:
    """Write the Markdown draft and the R10 JSON artifacts. Rows go to SQLite."""
    checked = _run_id(run_id)
    checked_kind = _kind(report_kind)
    draft = draft_report(
        evidence_pack,
        contradictions,
        trends,
        previous_evidence_ids=previous_evidence_ids,
        run_id=checked,
        report_kind=checked_kind,
    )
    filename = f"{checked_kind}.md"
    markdown_path = reports_dir / checked / filename
    _write_text(markdown_path, draft.markdown)
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    database_path = (
        database if database is not None else artifacts_dir / checked / "R10" / "reports.sqlite"
    )
    content_uri = f"reports/draft/{checked}/{filename}"
    body = _json_bytes(draft.document)
    claims_uri = object_store.put(f"{checked}/R10/report_claims.json", body)
    _save(database_path, draft, content_uri=content_uri, claims_uri=claims_uri)
    markdown_bytes = draft.markdown.encode("utf-8")
    manifest: dict[str, object] = {
        "ticket": "R10",
        "run_id": checked,
        "status": draft.status,
        "report_kind": checked_kind,
        "report_claims": "report_claims.json",
        "report_claims_sha256": hashlib.sha256(body).hexdigest(),
        "markdown_file": filename,
        "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
        "content_uri": content_uri,
        "claims_uri": claims_uri,
        "database": database_path.name,
        "template_version": TEMPLATE_VERSION,
        "prompt_model_version": PROMPT_MODEL_VERSION,
        "model_version": _MODEL_VERSION,
        "generated_by_ai": False,
        "evidence_ids": list(draft.evidence_ids),
    }
    object_store.put(f"{checked}/R10/manifest.json", _json_bytes(manifest))
    object_store.put(f"{checked}/R10/metrics.json", _json_bytes(draft.metrics))
    return draft


def _publish(
    claims: Sequence[_Claim],
    omissions: Sequence[Mapping[str, object]],
    links: Sequence[Mapping[str, object]],
    run_id: str | None,
    report_kind: str,
    evidence_ids: tuple[str, ...],
    *,
    compared: bool,
    new_ids: tuple[str, ...] | None,
) -> ReportDraft:
    document = _document(
        "success",
        run_id,
        report_kind,
        evidence_ids,
        error=None,
        claims=[claim.as_dict() for claim in claims],
        omissions=[dict(item) for item in omissions],
        links=[dict(item) for item in links],
    )
    markdown = _markdown(document, claims, omissions, links)
    metrics = _metrics(
        "success",
        claims,
        omissions,
        links,
        evidence_ids,
        compared=compared,
        new_ids=new_ids,
    )
    return ReportDraft("success", document, markdown, metrics, evidence_ids)


def _terminal(
    status: str,
    run_id: str | None,
    report_kind: str,
    evidence_ids: tuple[str, ...],
    *,
    error: str | None,
    compared: bool,
    new_ids: tuple[str, ...] | None,
    omissions: Sequence[Mapping[str, object]] = (),
) -> ReportDraft:
    stored = [dict(item) for item in omissions]
    document = _document(
        status,
        run_id,
        report_kind,
        evidence_ids,
        error=error,
        claims=[],
        omissions=stored,
        links=[],
    )
    markdown = _short_markdown(document, stored)
    metrics = _metrics(
        status,
        (),
        stored,
        (),
        evidence_ids,
        compared=compared,
        new_ids=new_ids,
        error=error,
    )
    return ReportDraft(status, document, markdown, metrics, evidence_ids)


def _assemble(
    evidence_pack: Mapping[str, object],
    contradictions: Mapping[str, object],
    trends: Sequence[Mapping[str, object]],
) -> tuple[list[_Claim], list[dict[str, object]], list[dict[str, object]]]:
    topics = _topics(evidence_pack, trends)
    valid = _valid_index(evidence_pack, contradictions)
    present = _pack_evidence_ids(evidence_pack)
    claims_in = _mappings(evidence_pack.get("claims"))
    chains = _mappings(contradictions.get("chains"))
    contradiction_rows = _mappings(contradictions.get("contradictions"))
    reprint_rows = _mappings(contradictions.get("reprints"))
    qa_rows = _mappings(contradictions.get("qa_queue"))
    blocked = _blocked_claims(claims_in, chains, contradiction_rows, qa_rows)
    built: list[_Claim] = []
    omissions: list[dict[str, object]] = []
    for claim in claims_in:
        _consume_claim(
            claim,
            topics,
            valid,
            present,
            blocked,
            contradiction_rows,
            qa_rows,
            built,
            omissions,
        )
    _consume_reprints(reprint_rows, built)
    _consume_incomplete(contradictions, claims_in, valid, built, omissions)
    built.sort(key=_claim_sort)
    links = [valid[key] for key in sorted(valid)]
    return built, omissions, links


def _consume_claim(
    claim: Mapping[str, object],
    topics: Mapping[str, str],
    valid: Mapping[str, Mapping[str, object]],
    present: set[str],
    blocked: set[str],
    contradiction_rows: Sequence[Mapping[str, object]],
    qa_rows: Sequence[Mapping[str, object]],
    built: list[_Claim],
    omissions: list[dict[str, object]],
) -> None:
    claim_id = _safe_id(claim.get("claim_id"))
    trend_id = _safe_id(claim.get("trend_id"))
    template = _label(claim.get("template")) or ""
    if claim_id is None or trend_id is None:
        omissions.append(_omission("", "", "", "claim_id_invalid"))
        return
    support = _support_ids(claim, valid)
    if claim.get("status") != "supported":
        omissions.append(_omission(claim_id, trend_id, template, "insufficient_evidence"))
        return
    if trend_id not in topics or template not in _FACT_TEMPLATES:
        reason = "topic_missing" if trend_id not in topics else "template_unsupported"
        omissions.append(_omission(claim_id, trend_id, template, reason))
        return
    if claim_id in blocked:
        _consume_blocked(
            claim_id,
            trend_id,
            template,
            contradiction_rows,
            qa_rows,
            built,
            omissions,
        )
        return
    referenced = _strings(claim.get("supporting_evidence_ids"))
    if any(evidence_id not in present for evidence_id in referenced):
        omissions.append(_omission(claim_id, trend_id, template, "evidence_not_in_pack"))
        return
    if not support:
        omissions.append(_omission(claim_id, trend_id, template, "evidence_not_schema_valid"))
        return
    rendered = _render_fact(
        template,
        topics[trend_id],
        _inputs(claim.get("inputs")),
        _window(claim),
    )
    if rendered is None:
        omissions.append(_omission(claim_id, trend_id, template, "inputs_missing"))
        return
    text, numbers = rendered
    section = _section_for(template)
    cited = _cite(text, support)
    built.append(
        _make(trend_id, claim_id, section, "fact", template, cited, support, numbers),
    )
    if template == "growth_7d":
        hypothesis = _hypothesis(topics[trend_id], numbers, support)
        if hypothesis is not None:
            built.append(
                _make(
                    trend_id,
                    claim_id,
                    "changes",
                    "hypothesis",
                    template,
                    hypothesis,
                    support,
                    numbers,
                )
            )


def _consume_blocked(
    claim_id: str,
    trend_id: str,
    template: str,
    contradiction_rows: Sequence[Mapping[str, object]],
    qa_rows: Sequence[Mapping[str, object]],
    built: list[_Claim],
    omissions: list[dict[str, object]],
) -> None:
    contradiction_ids = _row_ids(contradiction_rows, claim_id)
    qa_ids = _qa_ids(qa_rows, claim_id)
    if contradiction_ids:
        kinds = _kinds(contradiction_rows, claim_id)
        kind_text = "、".join(kinds) if kinds else "未标明类型"
        id_text = "、".join(contradiction_ids)
        text = f"反证：主张 {claim_id} 存在冲突（{kind_text}），不作为事实陈述。证据 {id_text}。"
        built.append(
            _make(
                trend_id,
                claim_id,
                "counter_evidence",
                "counter",
                "contradiction",
                text,
                contradiction_ids,
                (),
            )
        )
    if qa_ids:
        for qa_id, evidence_id, reason in _qa_items(qa_rows, claim_id):
            text = f"未知：QA 条目 {qa_id} 的原因是 {reason}，不作为事实陈述。证据 {evidence_id}。"
            built.append(
                _make(
                    trend_id,
                    qa_id,
                    "unknowns",
                    "unknown",
                    "qa",
                    text,
                    (evidence_id,),
                    (),
                )
            )
    if not contradiction_ids and not qa_ids:
        omissions.append(_omission(claim_id, trend_id, template, "conflict"))


def _consume_reprints(
    reprint_rows: Sequence[Mapping[str, object]],
    built: list[_Claim],
) -> None:
    for item in reprint_rows:
        trend_id = _safe_id(item.get("trend_id")) or ""
        source_claim = _safe_id(item.get("claim_id")) or ""
        reprint_id = _safe_id(item.get("evidence_id"))
        kept_id = _safe_id(item.get("kept_evidence_id"))
        ids = tuple(evidence_id for evidence_id in (reprint_id, kept_id) if evidence_id)
        if trend_id == "" or not ids:
            continue
        if reprint_id and kept_id:
            text = (
                f"同源转载：证据 {reprint_id} 与已保留证据 {kept_id} 属于同一来源簇，"
                "不作为独立反证，也不作为新的事实。"
            )
            source_id = source_claim or reprint_id
        else:
            only = reprint_id or kept_id or ""
            text = f"同源转载：证据 {only} 不作为独立反证，也不作为新的事实。"
            source_id = source_claim or only
        built.append(
            _make(
                trend_id,
                source_id,
                "counter_evidence",
                "counter",
                "reprint",
                text,
                ids,
                (),
            )
        )


def _consume_incomplete(
    contradictions: Mapping[str, object],
    claims: Sequence[Mapping[str, object]],
    valid: Mapping[str, Mapping[str, object]],
    built: list[_Claim],
    omissions: list[dict[str, object]],
) -> None:
    support_by_trend: dict[str, list[str]] = {}
    for claim in claims:
        trend_id = _safe_id(claim.get("trend_id"))
        if trend_id is None:
            continue
        for evidence_id in _support_ids(claim, valid):
            bucket = support_by_trend.setdefault(trend_id, [])
            if evidence_id not in bucket:
                bucket.append(evidence_id)
    for candidate in _mappings(contradictions.get("candidates")):
        if candidate.get("contrary_search_incomplete") is not True:
            continue
        trend_id = _safe_id(candidate.get("trend_id"))
        if trend_id is None:
            continue
        support = tuple(sorted(support_by_trend.get(trend_id, ())))
        if not support:
            omissions.append(_omission("", trend_id, "", "contrary_search_incomplete"))
            continue
        id_text = "、".join(support)
        text = (
            f"未知：趋势 {trend_id} 的反证检索未完成（contrary_search_incomplete），"
            f"不将其写成已无反证。证据 {id_text}。"
        )
        built.append(
            _make(
                trend_id,
                trend_id,
                "unknowns",
                "unknown",
                "contrary_search_incomplete",
                text,
                support,
                (),
            )
        )


def _render_fact(
    template: str,
    topic: str,
    inputs: Mapping[str, object],
    window: str,
) -> tuple[str, tuple[tuple[str, int | float], ...]] | None:
    if template == "growth_7d":
        previous = _stored_int(inputs.get("growth_previous"))
        current = _stored_int(inputs.get("growth_current"))
        if previous is None or current is None:
            return None
        text = f"主题 {topic} 在 UTC 7 日窗口内的独立记录数由 {previous} 变为 {current}{window}。"
        return text, (("growth_previous", previous), ("growth_current", current))
    if template == "growth_7d_unknown":
        previous = _stored_int(inputs.get("growth_previous"))
        if previous is None:
            return None
        text = (
            f"主题 {topic} 的 7 日增长率没有已存储的值；上一窗口的独立记录数为 {previous}{window}。"
        )
        return text, (("growth_previous", previous),)
    if template == "persistence_28d":
        numerator = _stored_int(inputs.get("persistence_numerator"))
        denominator = _stored_int(inputs.get("persistence_denominator"))
        if numerator is None or denominator is None:
            return None
        text = f"主题 {topic} 在 {denominator} 个 UTC 日中的 {numerator} 日有独立记录{window}。"
        return text, (
            ("persistence_numerator", numerator),
            ("persistence_denominator", denominator),
        )
    if template == "diversity_7d":
        numerator = _stored_int(inputs.get("diversity_numerator"))
        families = _families(inputs.get("source_families"))
        if numerator is None or families is None or numerator != len(families):
            return None
        listed = "、".join(families)
        text = f"主题 {topic} 的已存储来源族数量为 {numerator}（{listed}）{window}。"
        return text, (("diversity_numerator", numerator),)
    if template == "quality_7d":
        weight = _stored_number(inputs.get("quality_numerator"))
        denominator = _stored_int(inputs.get("quality_denominator"))
        if weight is None or denominator is None:
            return None
        shown = _format_number(weight)
        text = f"主题 {topic} 的已存储层级权重合计为 {shown}/{denominator}{window}。"
        return text, (("quality_numerator", weight), ("quality_denominator", denominator))
    if template == "developer_7d":
        numerator = _stored_int(inputs.get("developer_numerator"))
        denominator = _stored_int(inputs.get("developer_denominator"))
        if numerator is None or denominator is None:
            return None
        text = (
            f"主题 {topic} 的代码族记录为 {numerator} 条，对应 {denominator} 条独立记录{window}。"
        )
        return text, (("developer_numerator", numerator), ("developer_denominator", denominator))
    if template == "novelty_7d":
        numerator = _stored_int(inputs.get("novelty_numerator"))
        denominator = _stored_int(inputs.get("novelty_denominator"))
        if numerator is None or denominator is None:
            return None
        text = (
            f"主题 {topic} 在该窗口的 {denominator} 条独立记录中，"
            f"有 {numerator} 条使用窗口开始前未出现的键{window}。"
        )
        return text, (("novelty_numerator", numerator), ("novelty_denominator", denominator))
    return None


def _hypothesis(
    topic: str,
    numbers: Sequence[tuple[str, int | float]],
    evidence_ids: Sequence[str],
) -> str | None:
    stored = dict(numbers)
    previous = stored.get("growth_previous")
    current = stored.get("growth_current")
    if not isinstance(previous, int) or not isinstance(current, int):
        return None
    if current > previous:
        direction = "增加"
    elif current < previous:
        direction = "减少"
    else:
        direction = "持平"
    cited = "、".join(evidence_ids)
    return (
        f"hypothesis：将主题 {topic} 的独立记录数由 {previous} 变为 {current} "
        f"解释为{direction}。该句是推断，不是事实。证据 {cited}。"
    )


def _blocked_claims(
    claims: Sequence[Mapping[str, object]],
    chains: Sequence[Mapping[str, object]],
    contradiction_rows: Sequence[Mapping[str, object]],
    qa_rows: Sequence[Mapping[str, object]],
) -> set[str]:
    conflicted = {
        claim_id
        for chain in chains
        if chain.get("status") == "conflict"
        if (claim_id := _safe_id(chain.get("claim_id"))) is not None
    }
    conflicted.update(_claim_ids(contradiction_rows))
    conflicted.update(_claim_ids(qa_rows))
    return {
        claim_id
        for claim in claims
        if (claim_id := _safe_id(claim.get("claim_id"))) is not None and claim_id in conflicted
    }


def _markdown(
    document: Mapping[str, object],
    claims: Sequence[_Claim],
    omissions: Sequence[Mapping[str, object]],
    links: Sequence[Mapping[str, object]],
) -> str:
    lines = _header(document)
    grouped: dict[str, list[_Claim]] = {section: [] for section in _SECTION_ORDER}
    for claim in claims:
        grouped.setdefault(claim.section, []).append(claim)
    unknown_notes = [_omission_line(item) for item in omissions]
    for section in _SECTION_ORDER:
        extra = unknown_notes if section == "unknowns" else []
        lines.extend(_section_block(_SECTION_TITLE[section], grouped.get(section, ()), extra))
    lines.extend(["## 方法", "", _METHOD, "", "## 链接目录", ""])
    if links:
        for link in links:
            lines.append(_link_line(link))
    else:
        lines.append("无。")
    lines.append("")
    return "\n".join(lines)


def _short_markdown(
    document: Mapping[str, object],
    omissions: Sequence[Mapping[str, object]] = (),
) -> str:
    status = document.get("status")
    if status == "schema_error":
        sentence = "输入未通过结构检查，未生成事实陈述。"
    else:
        sentence = "本窗口没有新的证据，不生成事实陈述。"
    lines = _header(document)
    lines.extend([sentence, ""])
    for item in omissions:
        lines.extend([_omission_line(item), ""])
    return "\n".join(lines)


def _header(document: Mapping[str, object]) -> list[str]:
    kind = document.get("report_kind")
    title = "TechPulse 日报草稿" if kind == "daily" else "TechPulse 周报草稿"
    return [
        f"# {title}",
        "",
        f"- 模板版本：{TEMPLATE_VERSION}",
        f"- 提示词/模型版本：{PROMPT_MODEL_VERSION}",
        f"- 状态：{document.get('status')}",
        f"- report_id：{document.get('report_id')}",
        f"- run_id：{document.get('run_id')}",
        "",
    ]


def _section_block(
    title: str,
    claims: Sequence[_Claim],
    extra: Sequence[str],
) -> list[str]:
    lines = [f"## {title}", ""]
    if not claims and not extra:
        lines.extend(["无。", ""])
        return lines
    for claim in claims:
        lines.extend([claim.text, ""])
    for line in extra:
        lines.extend([line, ""])
    return lines


def _link_line(link: Mapping[str, object]) -> str:
    return " | ".join(
        (
            str(link.get("evidence_id") or ""),
            str(link.get("source_url") or ""),
            str(link.get("sha256") or ""),
            str(link.get("source_published_at") or ""),
            str(link.get("license_status") or ""),
            str(link.get("quality_tier") or ""),
        )
    )


def _document(
    status: str,
    run_id: str | None,
    report_kind: str,
    evidence_ids: tuple[str, ...],
    *,
    error: str | None,
    claims: list[dict[str, object]],
    omissions: list[dict[str, object]],
    links: list[dict[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "1.0",
        "ticket": "R10",
        "run_id": run_id,
        "report_id": _report_id(run_id, report_kind),
        "report_kind": report_kind,
        "status": status,
        "template_version": TEMPLATE_VERSION,
        "prompt_version": TEMPLATE_VERSION,
        "model_version": _MODEL_VERSION,
        "prompt_model_version": PROMPT_MODEL_VERSION,
        "generated_by_ai": False,
        "llm_calls": 0,
        "paid_api_calls": 0,
        "evidence_ids": list(evidence_ids),
        "claims": claims,
        "omissions": omissions,
        "links": links,
    }
    if error is not None:
        document["error"] = error
    return document


def _metrics(
    status: str,
    claims: Sequence[_Claim],
    omissions: Sequence[Mapping[str, object]],
    links: Sequence[Mapping[str, object]],
    evidence_ids: tuple[str, ...],
    *,
    compared: bool,
    new_ids: tuple[str, ...] | None,
    error: str | None = None,
) -> dict[str, object]:
    metrics: dict[str, object] = {
        "status": status,
        "template_version": TEMPLATE_VERSION,
        "prompt_model_version": PROMPT_MODEL_VERSION,
        "model_version": _MODEL_VERSION,
        "generated_by_ai": False,
        "claims": len(claims),
        "fact_claims": sum(1 for claim in claims if claim.kind == "fact"),
        "hypothesis_claims": sum(1 for claim in claims if claim.kind == "hypothesis"),
        "counter_claims": sum(1 for claim in claims if claim.kind == "counter"),
        "unknown_claims": sum(1 for claim in claims if claim.kind == "unknown"),
        "omissions": len(omissions),
        "links": len(links),
        "evidence_ids": len(evidence_ids),
        "compared_with_previous": compared,
        "new_evidence_ids": None if new_ids is None else len(new_ids),
        "llm_calls": 0,
        "paid_api_calls": 0,
        "third_party_writes": 0,
    }
    if error is not None:
        metrics["error"] = error
    return metrics


def _save(
    database: Path,
    draft: ReportDraft,
    *,
    content_uri: str,
    claims_uri: str,
) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_DDL)
        report_id = _text(draft.document.get("report_id")) or ""
        run_id = _text(draft.document.get("run_id")) or ""
        report_kind = _text(draft.document.get("report_kind")) or ""
        with connection:
            connection.execute("DELETE FROM report_claim_evidence")
            connection.execute("DELETE FROM report_claims")
            connection.execute("DELETE FROM report_drafts")
            connection.execute(
                """
                INSERT INTO report_drafts (
                    report_id, run_id, report_kind, status, template_version,
                    prompt_model_version, content_uri, claims_uri, generated_by_ai
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    run_id,
                    report_kind,
                    draft.status,
                    TEMPLATE_VERSION,
                    PROMPT_MODEL_VERSION,
                    content_uri,
                    claims_uri,
                    0,
                ),
            )
            for claim in _mappings(draft.document.get("claims")):
                claim_id = _text(claim.get("claim_id"))
                if claim_id is None:
                    continue
                connection.execute(
                    """
                    INSERT INTO report_claims (
                        claim_id, report_id, trend_id, source_claim_id, section,
                        kind, template, claim_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        report_id,
                        _text(claim.get("trend_id")) or "",
                        _text(claim.get("source_claim_id")) or "",
                        _text(claim.get("section")) or "",
                        _text(claim.get("kind")) or "",
                        _text(claim.get("template")) or "",
                        _text(claim.get("text")) or "",
                    ),
                )
                for evidence_id in _strings(claim.get("evidence_ids")):
                    connection.execute(
                        """
                        INSERT INTO report_claim_evidence (claim_id, evidence_id)
                        VALUES (?, ?)
                        """,
                        (claim_id, evidence_id),
                    )
    finally:
        connection.close()


def _structure_error(
    evidence_pack: Mapping[str, object],
    contradictions: Mapping[str, object],
    trends: Sequence[Mapping[str, object]],
) -> str | None:
    if _version(evidence_pack) not in {None, "1.0"}:
        return "evidence_pack_schema_version"
    for key in ("trends", "claims", "evidence"):
        if not isinstance(evidence_pack.get(key), list):
            return key
    if _version(contradictions) not in {None, "1.0"}:
        return "contradictions_schema_version"
    for key in ("candidates", "chains", "contradictions"):
        if not isinstance(contradictions.get(key), list):
            return key
    for key in ("reprints", "qa_queue"):
        value = contradictions.get(key)
        if value is not None and not isinstance(value, list):
            return key
    if isinstance(trends, str | bytes) or not isinstance(trends, Sequence):
        return "trends"
    return None


def _pack_evidence_ids(evidence_pack: Mapping[str, object]) -> set[str]:
    found: set[str] = set()
    for item in _mappings(evidence_pack.get("evidence")):
        _add_id(found, item.get("evidence_id"))
    return found


def _raw_ids(
    evidence_pack: Mapping[str, object],
    contradictions: Mapping[str, object],
) -> set[str]:
    present = _pack_evidence_ids(evidence_pack)
    found: set[str] = set(present)
    for key in ("contradictions", "reprints", "qa_queue"):
        for item in _mappings(contradictions.get(key)):
            for field in ("evidence_id", "kept_evidence_id"):
                evidence_id = item.get(field)
                if isinstance(evidence_id, str) and evidence_id in present:
                    _add_id(found, evidence_id)
    return found


def _valid_index(
    evidence_pack: Mapping[str, object],
    contradictions: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for item in _mappings(evidence_pack.get("evidence")):
        _put_valid(index, item)
    for key in ("contradictions", "reprints", "qa_queue"):
        for item in _mappings(contradictions.get(key)):
            _put_valid(index, item)
    return index


def _put_valid(index: dict[str, dict[str, object]], item: Mapping[str, object]) -> None:
    evidence_id = _safe_id(item.get("evidence_id"))
    if evidence_id is None or evidence_id in index:
        return
    payload = {key: item.get(key) for key in _EVIDENCE_FIELDS}
    try:
        parsed = EvidenceItem.model_validate(payload)
    except ValidationError:
        return
    published = item.get("source_published_at")
    collected = item.get("collected_at")
    if not isinstance(published, str) or not isinstance(collected, str):
        return
    index[evidence_id] = {
        "evidence_id": parsed.evidence_id,
        "canonical_id": parsed.canonical_id,
        "source_family": parsed.source_family.value,
        "source_url": parsed.source_url,
        "source_published_at": published,
        "collected_at": collected,
        "license_status": parsed.license_status.value,
        "quality_tier": parsed.quality_tier.value,
        "sha256": parsed.sha256,
    }


def _topics(
    evidence_pack: Mapping[str, object],
    trends: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    topics: dict[str, str] = {}
    for row in trends:
        if not isinstance(row, Mapping):
            continue
        _put_topic(topics, row)
    for row in _mappings(evidence_pack.get("trends")):
        _put_topic(topics, row)
    return topics


def _put_topic(topics: dict[str, str], row: Mapping[str, object]) -> None:
    trend_id = _safe_id(row.get("trend_id"))
    topic = _text(row.get("topic")) or _text(row.get("topic_id"))
    if trend_id is None or topic is None or _TOPIC.fullmatch(topic) is None:
        return
    topics[trend_id] = topic


def _support_ids(
    claim: Mapping[str, object],
    valid: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    found: list[str] = []
    for evidence_id in _strings(claim.get("supporting_evidence_ids")):
        if evidence_id in valid and evidence_id not in found:
            found.append(evidence_id)
    return tuple(sorted(found))


def _row_ids(rows: Sequence[Mapping[str, object]], claim_id: str) -> tuple[str, ...]:
    found: list[str] = []
    for item in rows:
        if _safe_id(item.get("claim_id")) != claim_id:
            continue
        if item.get("independent") is False:
            continue
        evidence_id = _safe_id(item.get("evidence_id"))
        if evidence_id is not None and evidence_id not in found:
            found.append(evidence_id)
    return tuple(sorted(found))


def _qa_ids(rows: Sequence[Mapping[str, object]], claim_id: str) -> tuple[str, ...]:
    return tuple(evidence_id for _qa_id, evidence_id, _reason in _qa_items(rows, claim_id))


def _qa_items(
    rows: Sequence[Mapping[str, object]],
    claim_id: str,
) -> tuple[tuple[str, str, str], ...]:
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in rows:
        if _safe_id(item.get("claim_id")) != claim_id:
            continue
        qa_id = _safe_id(item.get("qa_id"))
        evidence_id = _safe_id(item.get("evidence_id"))
        reason = _label(item.get("reason"))
        if qa_id is None or evidence_id is None or reason is None or qa_id in seen:
            continue
        seen.add(qa_id)
        found.append((qa_id, evidence_id, reason))
    return tuple(found)


def _kinds(rows: Sequence[Mapping[str, object]], claim_id: str) -> tuple[str, ...]:
    found: list[str] = []
    for item in rows:
        if _safe_id(item.get("claim_id")) != claim_id:
            continue
        for kind in _strings(item.get("kinds")):
            label = _label(kind)
            if label is not None and label not in found:
                found.append(label)
    return tuple(sorted(found))


def _claim_ids(rows: Sequence[Mapping[str, object]]) -> set[str]:
    return {claim_id for item in rows if (claim_id := _safe_id(item.get("claim_id"))) is not None}


def _make(
    trend_id: str,
    source_claim_id: str,
    section: str,
    kind: str,
    template: str,
    text: str,
    evidence_ids: Sequence[str],
    numbers: Sequence[tuple[str, int | float]],
) -> _Claim:
    ordered = tuple(sorted(dict.fromkeys(evidence_ids)))
    digest = hashlib.sha256(
        "\n".join((trend_id, source_claim_id, kind, section, ",".join(ordered))).encode()
    ).hexdigest()
    return _Claim(
        claim_id="rc_" + digest[:16],
        source_claim_id=source_claim_id,
        trend_id=trend_id,
        section=section,
        kind=kind,
        template=template,
        text=text,
        evidence_ids=ordered,
        numbers=tuple(numbers),
    )


def _omission(claim_id: str, trend_id: str, template: str, reason: str) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "trend_id": trend_id,
        "template": template,
        "reason": reason,
    }


def _omission_line(item: Mapping[str, object]) -> str:
    claim_id = _safe_id(item.get("claim_id")) or "未编号"
    reason = _label(item.get("reason")) or "unspecified"
    template = _label(item.get("template"))
    if template is not None:
        return f"未知：主张 {claim_id}（模板 {template}）不作为事实陈述（{reason}）。"
    return f"未知：主张 {claim_id} 不作为事实陈述（{reason}）。"


def _section_for(template: str) -> str:
    if template in _CHANGE_TEMPLATES:
        return "changes"
    if template in _UNKNOWN_TEMPLATES:
        return "unknowns"
    return "trends"


def _claim_sort(claim: _Claim) -> tuple[int, str, str, int]:
    return (
        _SECTION_ORDER.index(claim.section) if claim.section in _SECTION_ORDER else 99,
        claim.trend_id,
        claim.source_claim_id,
        _KIND_ORDER.get(claim.kind, 9),
    )


def _window(claim: Mapping[str, object]) -> str:
    start = _text(claim.get("window_start"))
    end = _text(claim.get("window_end"))
    if (
        start is None
        or end is None
        or _TIME.fullmatch(start) is None
        or _TIME.fullmatch(end) is None
    ):
        return ""
    return f"（{start} 至 {end}）"


def _families(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or _FAMILY.fullmatch(item) is None:
            return None
        names.append(item)
    return tuple(names)


def _cite(text: str, evidence_ids: Sequence[str]) -> str:
    return f"{text}证据 {'、'.join(evidence_ids)}。"


def _prior(values: Sequence[str] | None) -> set[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        raise TypeError("previous_evidence_ids must be a sequence of ids")
    found: set[str] = set()
    for item in values:
        _add_id(found, item)
    return found


def _add_id(found: set[str], value: object) -> None:
    if isinstance(value, str) and _ID.fullmatch(value):
        found.add(value)


def _inputs(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _stored_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _stored_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    return value


def _format_number(value: int | float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return format(float(value), ".10g")


def _version(value: Mapping[str, object]) -> str | None:
    stored = value.get("schema_version")
    if stored is None:
        return None
    return stored if isinstance(stored, str) else "invalid"


def _safe_id(value: object) -> str | None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        return None
    return value


def _label(value: object) -> str | None:
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        return None
    return value


def _kind(report_kind: str) -> str:
    if report_kind not in {"daily", "weekly"}:
        raise ValueError("invalid report_kind")
    return report_kind


def _report_id(run_id: str | None, report_kind: str) -> str:
    scope = run_id if run_id else "unscoped"
    return f"report:{scope}:{report_kind}"


def _mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            rows.append({str(key): entry for key, entry in item.items()})
    return rows


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and _ID.fullmatch(item)]


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped == "":
        return None
    return stripped


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
