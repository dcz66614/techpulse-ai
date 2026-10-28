"""Build the two G10 sample reports from the committed evidence fixture.

The script loads ``tests/fixtures/evidence/records.jsonl`` and runs the local
pipeline: D30 dedup, T20 features, T30 score, D40 entities, E10 pack, E20
review, R10 draft, R20 render, then Q10. It does not call a network, a model,
or a paid API, and it does not contact anyone. Facts, numbers, and URLs in
the samples are copied from those outputs. A sample is labelled publishable
only when Q10 sets ``qa_pass``. Any other result is labelled internal demo only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from techpulse.evidence.contradictions import review_contradictions
from techpulse.evidence.retrieve import clusters_from_dedup
from techpulse.evidence.verify import build_evidence_pack
from techpulse.normalize.dedup import deduplicate_records
from techpulse.normalize.entities import resolve_entities
from techpulse.reports.draft import draft_report
from techpulse.reports.qa import review_report
from techpulse.reports.render import render_topics
from techpulse.trends.features import build_features
from techpulse.trends.score import score_trends

RUN_ID = "g10-fixture"
FIXTURE_RELATIVE = Path("tests") / "fixtures" / "evidence" / "records.jsonl"
# The fixture records are published and collected against this close.
# tests/integration/test_e10_pack.py uses the same instant. A wall-clock
# as_of would move the windows and the samples would change.
FIXTURE_AS_OF = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
_SAMPLES = (
    ("sample_1", 7),
    ("sample_2", 28),
)
_PUBLISHABLE = "publishable"
_INTERNAL_DEMO = "internal demo only"


@dataclass(frozen=True)
class SampleBuild:
    name: str
    markdown: str
    q10_status: str
    qa_pass: bool
    publication_status: str
    report_id: str
    window_days: int
    release_status: str
    r10_status: str
    r20_status: str
    blockers: int


@dataclass(frozen=True)
class BuildResult:
    run_id: str
    samples: tuple[SampleBuild, ...]
    manifest: dict[str, object]
    metrics: dict[str, object]


def build_samples(root: Path) -> BuildResult:
    """Run the fixture pipeline and return the two samples plus G10 artifacts."""
    fixture = root / FIXTURE_RELATIVE
    records = _load_records(fixture)
    deduped = deduplicate_records(records)
    clusters = clusters_from_dedup(deduped.rows)
    features = build_features(records, as_of=FIXTURE_AS_OF)
    feature_rows = list(features.rows)
    scored = score_trends(feature_rows)
    if scored.status != "success":
        raise ValueError(f"score status {scored.status}")
    entities = [_object(row) for row in resolve_entities(deduped.rows).entities]
    samples: list[SampleBuild] = []
    for name, window_days in _SAMPLES:
        trends = [row for row in scored.trends if row.get("window_days") == window_days]
        if not trends:
            raise ValueError(f"no trend for window {window_days}")
        samples.append(
            _one_sample(
                name,
                window_days,
                trends,
                records,
                feature_rows,
                clusters,
                entities,
            )
        )
    built = tuple(samples)
    metrics = _metrics(fixture, built)
    manifest = _manifest(built, metrics)
    return BuildResult(RUN_ID, built, manifest, metrics)


def write_samples(root: Path, *, output_root: Path | None = None) -> BuildResult:
    """Write ``growth/sample_*.md`` and ``artifacts/{run_id}/G10``."""
    result = build_samples(root)
    destination = root if output_root is None else output_root
    for sample in result.samples:
        _write_text(destination / "growth" / f"{sample.name}.md", sample.markdown)
    g10 = destination / "artifacts" / result.run_id / "G10"
    _write_text(g10 / "metrics.json", _dumps(result.metrics))
    _write_text(g10 / "manifest.json", _dumps(result.manifest))
    return result


def main() -> int:
    """Write the samples and print the Q10 status of each one."""
    result = write_samples(Path(__file__).resolve().parents[1])
    summary = {
        "run_id": result.run_id,
        "status": result.metrics["status"],
        "samples": [
            {
                "name": sample.name,
                "publication_status": sample.publication_status,
                "q10_status": sample.q10_status,
                "qa_pass": sample.qa_pass,
            }
            for sample in result.samples
        ],
    }
    print(_dumps(summary), end="")
    return 0


def _one_sample(
    name: str,
    window_days: int,
    trends: Sequence[Mapping[str, object]],
    records: Sequence[Mapping[str, object]],
    features: Sequence[Mapping[str, object]],
    clusters: Mapping[str, str],
    entities: Sequence[Mapping[str, object]],
) -> SampleBuild:
    run_id = f"{RUN_ID}-{name.replace('_', '-')}"
    pack = build_evidence_pack(trends, records, features=features, clusters=clusters)
    review = review_contradictions(pack.document, records, clusters=clusters)
    draft = draft_report(
        pack.document,
        review.document,
        trends,
        run_id=run_id,
        report_kind="weekly",
    )
    rendered = render_topics(
        pack.document,
        review.document,
        trends,
        draft=draft.document,
        entities=entities,
        run_id=run_id,
    )
    qa = review_report(
        draft.document,
        pack.document,
        markdown=draft.markdown,
        features=features,
        raw_records=records,
        run_id=run_id,
    )
    q10_status = _required_text(qa.document, "status")
    if q10_status not in {"pass", "fail"}:
        raise ValueError(f"unexpected q10 status {q10_status}")
    qa_pass = qa.qa_pass
    if qa_pass != (q10_status == "pass"):
        raise ValueError("q10 status does not match qa_pass")
    publication = _PUBLISHABLE if qa_pass else _INTERNAL_DEMO
    report_id = _required_text(draft.document, "report_id")
    release_status = _required_text(qa.document, "release_status")
    blockers = qa.metrics.get("blockers")
    if isinstance(blockers, bool) or not isinstance(blockers, int):
        raise ValueError("q10 blockers")
    markdown = _sample_markdown(
        name,
        draft.markdown,
        rendered.markdown_files,
        pack.document,
        draft.document,
        rendered.cards,
        qa.document,
        features,
        publication,
    )
    return SampleBuild(
        name,
        markdown,
        q10_status,
        qa_pass,
        publication,
        report_id,
        window_days,
        release_status,
        draft.status,
        rendered.status,
        blockers,
    )


def _sample_markdown(
    name: str,
    draft_markdown: str,
    rendered_files: Mapping[str, str],
    pack: Mapping[str, object],
    draft: Mapping[str, object],
    cards: Mapping[str, object],
    qa: Mapping[str, object],
    features: Sequence[Mapping[str, object]],
    publication: str,
) -> str:
    topics = _topics(_rows(pack.get("trends")))
    as_of_values = {_as_of(features, topic) for topic in topics}
    if len(as_of_values) != 1:
        raise ValueError("sample topics do not share one as_of")
    as_of = next(iter(as_of_values))
    lines = [
        f"# TechPulse-AI 样报 {name}",
        "",
        "> **标注：AI 生成。**",
        f"> publication_status: {publication}",
        "> 本文件由已提交的本地 fixture 经 R10 草稿、R20 渲染与 Q10 审查后生成。",
        "> 事实、数字和链接只复制这些输出。本脚本不联系第三方。对外发送需要 AUTH-06。",
        "",
        "## 报告元数据",
        "",
        f"- sample_id: {name}",
        f"- report_id: {_required_text(draft, 'report_id')}",
        f"- report_type: {_required_text(draft, 'report_kind')}",
        f"- run_id: {_required_text(draft, 'run_id')}",
        f"- as_of: {as_of}",
        f"- evidence_pack_status: {_plain(pack.get('status'))}",
        f"- r10_status: {_plain(draft.get('status'))}",
        f"- r20_status: {_plain(cards.get('status'))}",
        f"- template_version: {_required_text(draft, 'template_version')}",
        f"- prompt_model_version: {_required_text(draft, 'prompt_model_version')}",
        f"- model_version: {_required_text(draft, 'model_version')}",
        f"- generated_by_ai: {_plain(draft.get('generated_by_ai'))}",
        f"- llm_calls: {_plain(qa.get('llm_calls'))}",
        f"- paid_api_calls: {_plain(qa.get('paid_api_calls'))}",
        f"- third_party_writes: {_plain(qa.get('third_party_writes'))}",
        f"- source_version: {_required_text(cards, 'source_version')}",
        f"- q10_status: {_required_text(qa, 'status')}",
        f"- qa_pass: {_plain(qa.get('qa_pass'))}",
        f"- release_status: {_required_text(qa, 'release_status')}",
        f"- publication_status: {publication}",
        "- ai_label: AI 生成",
        "",
    ]
    for trend in _rows(pack.get("trends")):
        lines.extend(
            [
                f"- trend_id: {_required_text(trend, 'trend_id')}",
                f"- topic: {_required_text(trend, 'topic')}",
                f"- window_days: {_plain(trend.get('window_days'))}",
                f"- trend_score: {_plain(trend.get('score'))}",
                f"- confidence: {_plain(trend.get('confidence'))}",
                f"- experimental: {_plain(trend.get('experimental'))}",
                f"- trend_status: {_plain(trend.get('status'))}",
            ]
        )
    lines.extend(["", "## R10 草稿", ""])
    lines.append(draft_markdown.rstrip("\n"))
    lines.extend(["", "## R20 渲染", ""])
    for filename in sorted(rendered_files):
        lines.extend([f"### {filename}", "", rendered_files[filename].rstrip("\n"), ""])
    lines.extend(["## Q10 结果", "", "```json", _dumps(qa).rstrip("\n"), "```", ""])
    return "\n".join(lines)


def _metrics(fixture: Path, samples: Sequence[SampleBuild]) -> dict[str, object]:
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    rows = [_sample_metrics(sample) for sample in samples]
    qa_pass = all(sample.qa_pass for sample in samples)
    return {
        "as_of": FIXTURE_AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fixture": FIXTURE_RELATIVE.as_posix(),
        "fixture_sha256": digest,
        "llm_calls": 0,
        "paid_api_calls": 0,
        "publication_blocked": not qa_pass,
        "run_id": RUN_ID,
        "samples": rows,
        "status": "pass" if qa_pass else "internal_demo",
        "third_party_contact": 0,
        "third_party_writes": 0,
        "ticket": "G10",
    }


def _sample_metrics(sample: SampleBuild) -> dict[str, object]:
    body = sample.markdown.encode("utf-8")
    return {
        "blockers": sample.blockers,
        "bytes": len(body),
        "file": f"growth/{sample.name}.md",
        "publication_status": sample.publication_status,
        "q10_status": sample.q10_status,
        "qa_pass": sample.qa_pass,
        "r10_status": sample.r10_status,
        "r20_status": sample.r20_status,
        "release_status": sample.release_status,
        "report_id": sample.report_id,
        "sha256": hashlib.sha256(body).hexdigest(),
        "window_days": sample.window_days,
    }


def _manifest(samples: Sequence[SampleBuild], metrics: Mapping[str, object]) -> dict[str, object]:
    metrics_body = _dumps(metrics).encode("utf-8")
    artifacts: list[dict[str, object]] = [
        {
            "bytes": len(metrics_body),
            "path": f"artifacts/{RUN_ID}/G10/metrics.json",
            "sha256": hashlib.sha256(metrics_body).hexdigest(),
        }
    ]
    for sample in samples:
        body = sample.markdown.encode("utf-8")
        artifacts.append(
            {
                "bytes": len(body),
                "path": f"growth/{sample.name}.md",
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    artifacts.sort(key=lambda item: str(item["path"]))
    return {
        "artifacts": artifacts,
        "auth06_required_for_external_send": True,
        "publication_blocked": metrics["publication_blocked"],
        "run_id": RUN_ID,
        "status": metrics["status"],
        "ticket": "G10",
    }


def _load_records(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded: object = json.loads(line)
        rows.append(_object(loaded))
    if not rows:
        raise ValueError("fixture is empty")
    return rows


def _topics(trends: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    found = {_required_text(trend, "topic") for trend in trends}
    if not found:
        raise ValueError("pack has no topic")
    return tuple(sorted(found))


def _as_of(features: Sequence[Mapping[str, object]], topic: str) -> str:
    for row in features:
        if row.get("topic_id") == topic:
            return _required_text(row, "as_of")
    raise ValueError(f"no feature row for {topic}")


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return [_object(item) for item in value]


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return {str(key): item for key, item in value.items()}


def _required_text(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(key)
    return value


def _plain(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    raise ValueError(f"unsupported value {value!r}")


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
