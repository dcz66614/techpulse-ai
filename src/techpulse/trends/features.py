"""T20 7-day and 28-day growth features.

Windows are UTC calendar days, half-open at midnight. Counts are distinct
independent records. A growth rate is omitted when the previous window has
fewer than ``MIN_BASELINE`` records, so a small base is not turned into a
percentage. Numerators, denominators, and the record ids behind them are
stored on the feature row.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from techpulse.workflows.activities import LocalObjectStore, ObjectStore

MIN_BASELINE = 3
FAMILY_COUNT = 4
PENALTY_CAP = 0.30
PENALTY_UNIT = 0.10
BOT_MIN_RECORDS = 5
BOT_SHARE = 0.8
PARQUET_NOTE = "pyarrow is not installed; wrote {stem}.jsonl instead of {stem}.parquet"
_TIER_WEIGHT = {"A": 1.0, "B": 0.5, "C": 0.0}
_FAMILY_BY_SOURCE = {
    "openalex": "academic",
    "crossref": "academic",
    "arxiv": "academic",
    "github": "code",
    "hn": "community",
    "rss": "news",
    "sec": "news",
    "cninfo": "news",
}
_FAMILY_ALIASES = {
    "academic": "academic",
    "academic_papers": "academic",
    "code": "code",
    "code_repositories": "code",
    "community": "community",
    "community_discussions": "community",
    "news": "news",
    "syndicated_feeds": "news",
    "regulatory_filings": "news",
}
_DDL = """
CREATE TABLE IF NOT EXISTS trend_feature_snapshots (
    topic_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (topic_id, as_of)
);
"""


@dataclass(frozen=True)
class FeatureResult:
    rows: tuple[dict[str, object], ...]
    input_rows: int
    rejected_time: int
    rejected_topic: int


@dataclass(frozen=True)
class _Record:
    record_id: str
    topic_id: str
    published: datetime
    source_id: str
    family: str | None
    key: str
    tier: str | None
    host: str | None
    domain: str | None
    bot: bool


def window_bounds(as_of: datetime, days: int) -> tuple[datetime, datetime]:
    """Half-open UTC range covering ``days`` natural days ending on ``as_of``'s date."""
    if days < 1:
        raise ValueError("days must be positive")
    end_date = as_of.astimezone(UTC).date()
    start_date = end_date - timedelta(days=days - 1)
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=UTC) + timedelta(days=1)
    return start, end


def previous_window(as_of: datetime, days: int) -> tuple[datetime, datetime]:
    """The ``days`` UTC natural days immediately before :func:`window_bounds`."""
    start, _end = window_bounds(as_of, days)
    return start - timedelta(days=days), start


def build_features(
    records: Sequence[Mapping[str, object]],
    *,
    as_of: datetime,
) -> FeatureResult:
    """Aggregate one feature row per topic. ``as_of`` selects the UTC day that closes the window."""
    accepted, rejected_time, rejected_topic = _accept(records)
    grouped: dict[str, list[_Record]] = {}
    for record in accepted:
        grouped.setdefault(record.topic_id, []).append(record)
    current_7 = window_bounds(as_of, 7)
    previous_7 = previous_window(as_of, 7)
    current_28 = window_bounds(as_of, 28)
    rows = tuple(
        _topic_row(topic_id, grouped[topic_id], as_of, current_7, previous_7, current_28)
        for topic_id in sorted(grouped)
    )
    return FeatureResult(rows, len(records), rejected_time, rejected_topic)


def run_features(
    records: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    as_of: datetime,
    store: ObjectStore | None = None,
    database: Path | None = None,
) -> FeatureResult:
    """Write ``{run_id}/T20`` features, manifest, and metrics. Rows also go to SQLite."""
    result = build_features(records, as_of=as_of)
    database_path = (
        database if database is not None else artifacts_dir / run_id / "T20" / "features.sqlite"
    )
    _save(database_path, result.rows)
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    body = _jsonl(result.rows)
    name, output_format, note, data = _write_table(
        object_store, run_id, "features", result.rows, body
    )
    unknown_growth = sum(1 for row in result.rows if row.get("growth_unknown") is True)
    metrics: dict[str, object] = {
        "input_rows": result.input_rows,
        "topics": len(result.rows),
        "rejected_time": result.rejected_time,
        "rejected_topic": result.rejected_topic,
        "unknown_growth": unknown_growth,
        "output_format": output_format,
        "note": note,
        "third_party_writes": 0,
        "status": "success",
    }
    manifest: dict[str, object] = {
        "ticket": "T20",
        "run_id": run_id,
        "status": "success",
        "as_of": _iso(as_of),
        "features": name,
        "features_sha256": hashlib.sha256(data).hexdigest(),
        "output_format": output_format,
        "note": note,
        "database": database_path.name,
        "min_baseline": MIN_BASELINE,
    }
    object_store.put(f"{run_id}/T20/metrics.json", _json_bytes(metrics))
    object_store.put(f"{run_id}/T20/manifest.json", _json_bytes(manifest))
    return result


def _topic_row(
    topic_id: str,
    records: Sequence[_Record],
    as_of: datetime,
    current_7: tuple[datetime, datetime],
    previous_7: tuple[datetime, datetime],
    current_28: tuple[datetime, datetime],
) -> dict[str, object]:
    raw_7, independent_7 = _in_window(records, current_7)
    raw_prev, independent_prev = _in_window(records, previous_7)
    raw_28, independent_28 = _in_window(records, current_28)
    current = len(independent_7)
    previous = len(independent_prev)
    growth_unknown = previous < MIN_BASELINE
    numerator = current - previous
    normalized: float | None
    if growth_unknown:
        normalized = None
    else:
        normalized = _clip(numerator / previous)
    families = sorted({record.family for record in independent_7 if record.family is not None})
    quality, quality_numerator, quality_denominator, quality_unknown = _quality(independent_7)
    novelty, novelty_numerator, novelty_denominator, novelty_unknown = _novelty(
        independent_7, records, current_7[0]
    )
    active_dates = sorted({record.published.date().isoformat() for record in independent_28})
    developer, developer_numerator, developer_denominator, developer_unknown = _developer(
        independent_7
    )
    penalty, reasons = _penalty(raw_7, independent_7, current, previous)
    return {
        "topic_id": topic_id,
        "as_of": _iso(as_of),
        "window_7d_start": _iso(current_7[0]),
        "window_7d_end": _iso(current_7[1]),
        "window_7d_previous_start": _iso(previous_7[0]),
        "window_7d_previous_end": _iso(previous_7[1]),
        "window_28d_start": _iso(current_28[0]),
        "window_28d_end": _iso(current_28[1]),
        "record_ids_7d": _ids(raw_7),
        "independent_ids_7d": _ids(independent_7),
        "record_ids_7d_previous": _ids(raw_prev),
        "independent_ids_7d_previous": _ids(independent_prev),
        "record_ids_28d": _ids(raw_28),
        "independent_ids_28d": _ids(independent_28),
        "growth_current": current,
        "growth_previous": previous,
        "growth_numerator": numerator,
        "growth_denominator": previous,
        "normalized_growth_7d": normalized,
        "growth_unknown": growth_unknown,
        "small_sample": growth_unknown,
        "missing_baseline": previous == 0,
        "cross_source_diversity": len(families) / FAMILY_COUNT,
        "diversity_numerator": len(families),
        "diversity_denominator": FAMILY_COUNT,
        "source_families": families,
        "source_quality": quality,
        "quality_numerator": quality_numerator,
        "quality_denominator": quality_denominator,
        "quality_unknown": quality_unknown,
        "novelty": novelty,
        "novelty_numerator": novelty_numerator,
        "novelty_denominator": novelty_denominator,
        "novelty_unknown": novelty_unknown,
        "persistence_28d": len(active_dates) / 28,
        "persistence_numerator": len(active_dates),
        "persistence_denominator": 28,
        "active_dates_28d": active_dates,
        "developer_activity": developer,
        "developer_numerator": developer_numerator,
        "developer_denominator": developer_denominator,
        "developer_unknown": developer_unknown,
        "anomaly_penalty": penalty,
        "anomaly_reasons": reasons,
    }


def _quality(
    records: Sequence[_Record],
) -> tuple[float | None, float | None, int | None, bool]:
    weighted = [record for record in records if record.tier in _TIER_WEIGHT]
    if not weighted:
        return None, None, None, True
    numerator = sum(_TIER_WEIGHT[record.tier] for record in weighted if record.tier is not None)
    return numerator / len(weighted), numerator, len(weighted), False


def _novelty(
    current: Sequence[_Record],
    records: Sequence[_Record],
    window_start: datetime,
) -> tuple[float | None, int | None, int | None, bool]:
    if not current:
        return None, None, None, True
    seen = {record.key for record in records if record.published < window_start}
    numerator = sum(1 for record in current if record.key not in seen)
    return numerator / len(current), numerator, len(current), False


def _developer(records: Sequence[_Record]) -> tuple[float | None, int | None, int | None, bool]:
    if not records:
        return None, None, None, True
    numerator = sum(1 for record in records if record.family == "code")
    return numerator / len(records), numerator, len(records), False


def _penalty(
    raw: Sequence[_Record],
    independent: Sequence[_Record],
    current: int,
    previous: int,
) -> tuple[float, list[dict[str, object]]]:
    reasons: list[dict[str, object]] = []
    total = 0.0
    if raw and len(raw) > len(independent):
        ratio = (len(raw) - len(independent)) / len(raw)
        total += PENALTY_UNIT * ratio
        reasons.append(
            {
                "code": "reprint",
                "metric": "reprint_ratio",
                "raw_count": len(raw),
                "independent_count": len(independent),
                "value": ratio,
            }
        )
    domain_reason = _same_domain(independent)
    if domain_reason is not None:
        total += PENALTY_UNIT
        reasons.append(domain_reason)
    bot_reason = _bot(raw)
    if bot_reason is not None:
        total += PENALTY_UNIT
        reasons.append(bot_reason)
    if previous < MIN_BASELINE and current > previous:
        total += PENALTY_UNIT
        reasons.append(
            {
                "code": "small_base",
                "metric": "previous_independent_7d",
                "value": previous,
                "current_independent_7d": current,
            }
        )
    return min(total, PENALTY_CAP), reasons


def _same_domain(records: Sequence[_Record]) -> dict[str, object] | None:
    grouped: dict[str, list[_Record]] = {}
    for record in records:
        if record.domain is None or record.host is None:
            continue
        grouped.setdefault(record.domain, []).append(record)
    best: tuple[int, str, int] | None = None
    for domain, rows in grouped.items():
        hosts = {row.host for row in rows if row.host is not None}
        if len(rows) < 2 or len(hosts) < 2:
            continue
        candidate = (len(rows), domain, len(hosts))
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return None
    count, domain, host_count = best
    return {
        "code": "same_domain",
        "metric": "domain_record_count",
        "domain": domain,
        "host_count": host_count,
        "value": count,
    }


def _bot(records: Sequence[_Record]) -> dict[str, object] | None:
    if not records:
        return None
    flagged = sum(1 for record in records if record.bot)
    if flagged > 0 and flagged / len(records) >= BOT_SHARE:
        return {
            "code": "bot",
            "metric": "bot_share",
            "raw_count": len(records),
            "bot_count": flagged,
            "value": flagged / len(records),
        }
    counts: dict[str, int] = {}
    for record in records:
        if record.source_id:
            counts[record.source_id] = counts.get(record.source_id, 0) + 1
    if not counts:
        return None
    source_id, count = max(counts.items(), key=lambda item: (item[1], item[0]))
    share = count / len(records)
    if count >= BOT_MIN_RECORDS and share >= BOT_SHARE:
        return {
            "code": "bot",
            "metric": "source_share",
            "source_id": source_id,
            "raw_count": len(records),
            "source_count": count,
            "value": share,
        }
    return None


def _in_window(
    records: Sequence[_Record],
    bounds: tuple[datetime, datetime],
) -> tuple[list[_Record], list[_Record]]:
    start, end = bounds
    raw = [record for record in records if start <= record.published < end]
    raw.sort(key=lambda record: (record.published, record.record_id))
    seen: set[str] = set()
    independent: list[_Record] = []
    for record in raw:
        if record.key in seen:
            continue
        seen.add(record.key)
        independent.append(record)
    return raw, independent


def _accept(
    records: Sequence[Mapping[str, object]],
) -> tuple[list[_Record], int, int]:
    accepted: list[_Record] = []
    seen: set[str] = set()
    rejected_time = 0
    rejected_topic = 0
    for record in records:
        topic_id = record.get("topic_id")
        if not isinstance(topic_id, str) or not topic_id.strip():
            rejected_topic += 1
            continue
        published = _parse_time(record.get("published_at"))
        if published is None:
            rejected_time += 1
            continue
        record_id = _record_id(record)
        if record_id is None or record_id in seen:
            rejected_topic += 1
            continue
        seen.add(record_id)
        source_id = record.get("source_id")
        source = source_id.strip() if isinstance(source_id, str) else ""
        host, domain = _host_domain(record.get("canonical_url"))
        accepted.append(
            _Record(
                record_id=record_id,
                topic_id=topic_id.strip(),
                published=published,
                source_id=source,
                family=_family(record, source),
                key=_independent_key(record, record_id),
                tier=_tier(record.get("quality_tier")),
                host=host,
                domain=domain,
                bot=record.get("bot") is True,
            )
        )
    return accepted, rejected_time, rejected_topic


def _family(record: Mapping[str, object], source_id: str) -> str | None:
    declared = record.get("source_family")
    if isinstance(declared, str):
        mapped = _FAMILY_ALIASES.get(declared.strip().lower())
        if mapped is not None:
            return mapped
    return _FAMILY_BY_SOURCE.get(source_id.strip().lower())


def _independent_key(record: Mapping[str, object], record_id: str) -> str:
    for field in ("upstream_id", "canonical_id", "content_sha256"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return field + ":" + value.strip()
    return "record:" + record_id


def _tier(value: object) -> str | None:
    if isinstance(value, str) and value.strip().upper() in _TIER_WEIGHT:
        return value.strip().upper()
    return None


def _record_id(record: Mapping[str, object]) -> str | None:
    value = record.get("record_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    source = record.get("source_id")
    source_record = record.get("source_record_id")
    if isinstance(source, str) and isinstance(source_record, str) and source and source_record:
        return f"{source}:{source_record}"
    return None


def _host_domain(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    host = urlparse(value.strip()).hostname
    if host is None:
        return None, None
    host = host.lower().removeprefix("www.")
    labels = [label for label in host.split(".") if label]
    if len(labels) >= 2:
        return host, ".".join(labels[-2:])
    return host, host


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


def _ids(records: Sequence[_Record]) -> list[str]:
    return sorted(record.record_id for record in records)


def _clip(value: float) -> float:
    return min(1.0, max(0.0, value))


def _iso(value: datetime) -> str:
    text = value.astimezone(UTC).isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _save(database: Path, rows: Sequence[Mapping[str, object]]) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(_DDL)
        with connection:
            connection.execute("DELETE FROM trend_feature_snapshots")
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO trend_feature_snapshots (topic_id, as_of, payload)
                    VALUES (?, ?, ?)
                    """,
                    (
                        str(row["topic_id"]),
                        str(row["as_of"]),
                        json.dumps(row, ensure_ascii=False, sort_keys=True),
                    ),
                )
    finally:
        connection.close()


def _jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_table(
    store: ObjectStore,
    run_id: str,
    stem: str,
    rows: Sequence[Mapping[str, object]],
    body: bytes,
) -> tuple[str, str, str, bytes]:
    if importlib.util.find_spec("pyarrow") is None:
        name = f"{stem}.jsonl"
        store.put(f"{run_id}/T20/{name}", body)
        return name, "jsonl", PARQUET_NOTE.format(stem=stem), body
    name = f"{stem}.parquet"
    data = _parquet_bytes(rows)
    store.put(f"{run_id}/T20/{name}", data)
    return name, "parquet", "", data


def _parquet_bytes(rows: Sequence[Mapping[str, object]]) -> bytes:
    import io
    from typing import Any

    pyarrow: Any = __import__("pyarrow")
    parquet: Any = __import__("pyarrow.parquet", fromlist=["parquet"])
    buffer = io.BytesIO()
    parquet.write_table(pyarrow.Table.from_pylist([dict(row) for row in rows]), buffer)
    data = buffer.getvalue()
    if not isinstance(data, bytes):
        raise RuntimeError("parquet writer returned no bytes")
    return data
