"""Regression tests for SRCfix (TechPulse-AI P0004).

Covers:
1. OpenAlex: 401 on later page / missing params (page 1 succeeds, page 2 401 ends pagination gracefully; api_key param handling).
2. Crossref: 406 request shape (Accept: application/json succeeds; legacy vnd header returns 406).
3. arXiv: Atom XML false-positive captcha + transient 5xx retry with backoff and 3s minimum.
4. Orchestrator: disable -> re-probe -> re-enable (transient/recovered re-enables; genuine block stays disabled).
5. Soak cadence: wall-clock scheduling after simulated suspend (short sleep slices, instant resume, 1 cycle, no burst catch-up).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import pytest

from techpulse.collectors.arxiv import collect_arxiv
from techpulse.collectors.base import CursorStore, ManualClock, load_source_config
from techpulse.collectors.crossref import collect_crossref
from techpulse.collectors.openalex import collect_openalex
from techpulse.contracts import RawRecord, TaskStatus
from techpulse.ops.soak import SoakRunner
from techpulse.workflows.activities import ActivityContext, ActivityResult
from techpulse.workflows.orchestrator import (
    LocalOrchestrator,
    ManualClock as OrchestratorClock,
    pipeline_task_ids,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "source_registry.yaml"
WHEN = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. OpenAlex: 401 on later page / missing params
# ---------------------------------------------------------------------------


def test_openalex_401_on_later_page_preserves_first_page_and_warns() -> None:
    """When page 1 succeeds and page 2 returns 401 (e.g. key required for cursor),

    the collector preserves page 1 records, stops pagination, logs a warning,
    and returns SUCCESS (does not fail the whole cycle as policy_blocked).
    """
    page1_body = {
        "meta": {"count": 200, "page": 1, "per_page": 2, "next_cursor": "ILS_cursor_page2"},
        "results": [
            {
                "id": "https://openalex.org/W1111111111",
                "display_name": "Work One on AI",
                "publication_date": "2026-01-01",
            },
            {
                "id": "https://openalex.org/W2222222222",
                "display_name": "Work Two on AI",
                "publication_date": "2026-01-02",
            },
        ],
    }
    page2_body = {
        "error": "API key required for cursor paging",
        "message": "Paging past the first page with cursor= needs a free API key.",
    }

    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        cursor = request.url.params.get("cursor")
        if cursor == "*":
            return httpx.Response(200, json=page1_body)
        return httpx.Response(401, json=page2_body)

    transport = httpx.MockTransport(handler)

    with TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "openalex.sqlite"
        with CursorStore(store_path) as store:
            result = collect_openalex(
                keyword="artificial intelligence",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                per_page=2,
                collected_at=WHEN,
            )

            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 2
            assert len(result.records) == 2
            assert {r.source_record_id for r in result.records} == {"W1111111111", "W2222222222"}
            assert store.seen_count("openalex") == 2
            assert any("stopping pagination after page 1" in w for w in result.warnings)

    assert len(seen_requests) == 2


def test_openalex_missing_params_and_api_key_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAlex supports optional OPENALEX_API_KEY from env or argument."""
    single_page = {
        "meta": {"count": 1, "page": 1, "per_page": 1, "next_cursor": None},
        "results": [
            {
                "id": "https://openalex.org/W3333333333",
                "display_name": "Work Three",
                "publication_date": "2026-01-01",
            }
        ],
    }

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=single_page)

    transport = httpx.MockTransport(handler)

    # 1. Without API key in env or args
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    with TemporaryDirectory() as tmp:
        with CursorStore(Path(tmp) / "c1.sqlite") as store:
            res1 = collect_openalex(
                keyword="robotics",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                collected_at=WHEN,
            )
            assert res1.status is TaskStatus.SUCCESS
            assert "api_key" not in seen[0].url.params
            assert "mailto" in seen[0].url.params

    # 2. With API key in env
    monkeypatch.setenv("OPENALEX_API_KEY", "test_free_key_123")
    seen.clear()
    with TemporaryDirectory() as tmp:
        with CursorStore(Path(tmp) / "c2.sqlite") as store:
            res2 = collect_openalex(
                keyword="robotics",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                collected_at=WHEN,
            )
            assert res2.status is TaskStatus.SUCCESS
            assert seen[0].url.params["api_key"] == "test_free_key_123"

    # 3. Explicit argument overrides env
    seen.clear()
    with TemporaryDirectory() as tmp:
        with CursorStore(Path(tmp) / "c3.sqlite") as store:
            res3 = collect_openalex(
                keyword="robotics",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                api_key="arg_key_456",
                collected_at=WHEN,
            )
            assert res3.status is TaskStatus.SUCCESS
            assert seen[0].url.params["api_key"] == "arg_key_456"


def test_openalex_genuine_401_on_page_one_fails_closed() -> None:
    """Genuine 401 on initial request (0 records) fails honestly as policy_blocked."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"error": "Unauthorized access"}')

    with TemporaryDirectory() as tmp:
        with CursorStore(Path(tmp) / "c.sqlite") as store:
            result = collect_openalex(
                keyword="test",
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.POLICY_BLOCKED
            assert result.records == []
            assert result.errors == ["policy_blocked: 401"]


# ---------------------------------------------------------------------------
# 2. Crossref: 406 request shape
# ---------------------------------------------------------------------------


def test_crossref_406_request_shape_fixed() -> None:
    """Crossref server returns 406 when Accept is application/vnd.crossref-api-message+json,

    and 200 when Accept is application/json. Production code must build Accept: application/json.
    """
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        accept = request.headers.get("accept", "")
        if "application/vnd.crossref-api-message+json" in accept:
            return httpx.Response(406, text="No acceptable resource available.")
        if "application/json" in accept:
            body = {
                "status": "ok",
                "message": {
                    "total-results": 1,
                    "items": [
                        {
                            "DOI": "10.5555/sample.406.fix",
                            "title": ["Curing 406 on Crossref Works API"],
                            "URL": "https://doi.org/10.5555/sample.406.fix",
                            "created": {"date-parts": [[2026, 1, 1]]},
                        }
                    ],
                },
            }
            return httpx.Response(200, json=body)
        return httpx.Response(406, text="Unrecognized accept")

    with TemporaryDirectory() as tmp:
        with CursorStore(Path(tmp) / "crossref.sqlite") as store:
            result = collect_crossref(
                query="sample",
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                rows=1,
                max_pages=1,
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 1
            assert result.records[0].source_record_id == "10.5555/sample.406.fix"

    assert len(seen_headers) == 1
    assert seen_headers[0]["accept"] == "application/json"


# ---------------------------------------------------------------------------
# 3. arXiv: Atom XML false-positive captcha + transient 5xx retry
# ---------------------------------------------------------------------------


def test_arxiv_atom_xml_abstract_mentioning_captcha_is_not_blocked() -> None:
    """An Atom XML response containing the word 'captcha' in paper text must NOT

    be classified as a captcha block.
    """
    atom_feed = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>1</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>1</opensearch:itemsPerPage>
  <entry>
    <id>http://arxiv.org/abs/2601.12345v1</id>
    <title>Deep Learning Attacks on Modern Audio and Text CAPTCHA Schemes</title>
    <summary>We present a robust deep learning framework to break audio reCAPTCHA and hCaptcha challenges with 99% accuracy.</summary>
    <published>2026-01-15T00:00:00Z</published>
    <updated>2026-01-15T00:00:00Z</updated>
    <author><name>Alice Researcher</name></author>
  </entry>
</feed>
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/atom+xml"},
            text=atom_feed,
        )

    with TemporaryDirectory() as tmp:
        with CursorStore(Path(tmp) / "arxiv.sqlite") as store:
            result = collect_arxiv(
                search_query="cat:cs.AI",
                window_start=datetime(2026, 1, 1, tzinfo=UTC),
                window_end=datetime(2026, 2, 1, tzinfo=UTC),
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                page_size=1,
                max_pages=1,
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 1
            assert result.records[0].source_record_id == "2601.12345v1"
            assert "CAPTCHA" in result.records[0].title


def test_arxiv_transient_5xx_retries_with_backoff_and_succeeds() -> None:
    """Transient 500/503 from export.arxiv.org retries with bounded retry and

    respects the >=3 second minimum between requests.
    """
    atom_feed = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2601.99999v1</id>
    <title>Resilient Scientific Ingestion</title>
    <published>2026-01-15T00:00:00Z</published>
    <updated>2026-01-15T00:00:00Z</updated>
    <author><name>Bob</name></author>
  </entry>
</feed>
"""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="Service Temporarily Unavailable")
        if attempts == 2:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, headers={"content-type": "application/atom+xml"}, text=atom_feed)

    clock = ManualClock()
    with TemporaryDirectory() as tmp:
        with CursorStore(Path(tmp) / "arxiv_retry.sqlite") as store:
            result = collect_arxiv(
                search_query="cat:cs.AI",
                window_start=datetime(2026, 1, 1, tzinfo=UTC),
                window_end=datetime(2026, 2, 1, tzinfo=UTC),
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                page_size=1,
                max_pages=1,
                max_retries=3,
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 1
            assert attempts == 3
            # arXiv enforces >= 3.0 seconds floor between attempts
            assert len(clock.slept) >= 2
            assert all(s >= 3.0 for s in clock.slept)


def test_arxiv_genuine_html_captcha_is_policy_blocked() -> None:
    """A genuine HTML captcha challenge page is still classified as policy_blocked."""
    html_captcha = """<!DOCTYPE html>
<html>
<head><title>Bot Check - Captcha Required</title></head>
<body>
  <div class="cf-challenge">
    <h1>Security Check</h1>
    <p>Please complete the captcha to access arXiv.</p>
  </div>
</body>
</html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"content-type": "text/html"}, text=html_captcha)

    with TemporaryDirectory() as tmp:
        with CursorStore(Path(tmp) / "c.sqlite") as store:
            result = collect_arxiv(
                search_query="cat:cs.AI",
                window_start=datetime(2026, 1, 1, tzinfo=UTC),
                window_end=datetime(2026, 2, 1, tzinfo=UTC),
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.POLICY_BLOCKED
            assert result.errors == ["policy_blocked: captcha"]


# ---------------------------------------------------------------------------
# 4. Orchestrator: disable -> re-probe -> re-enable
# ---------------------------------------------------------------------------


def test_orchestrator_disable_reprobe_reenable() -> None:
    """A source auto-disabled in cycle 1 is re-probed after cooldown in cycle 2

    and re-enabled if the probe succeeds. If probe fails with genuine 403, it stays disabled.
    """
    call_counts: dict[str, int] = {}
    should_succeed = {"openalex": False}

    def activity(context: ActivityContext) -> ActivityResult:
        tid = context.task_id
        call_counts[tid] = call_counts.get(tid, 0) + 1
        if tid == "C10":
            if not should_succeed["openalex"]:
                return ActivityResult(
                    status=TaskStatus.POLICY_BLOCKED,
                    code="http_403",
                    source_id="openalex",
                )
            return ActivityResult(
                status=TaskStatus.SUCCESS,
                source_id="openalex",
                records=("W_probed_ok",),
            )
        return ActivityResult(
            status=TaskStatus.SUCCESS,
            records=(f"{tid}_ok",),
        )

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "o20.sqlite"
        clock = OrchestratorClock(WHEN)

        with LocalOrchestrator(
            db_path,
            clock=clock,
            activities={tid: activity for tid in pipeline_task_ids()},
            probe_cooldown_seconds=60.0,
        ) as orch:
            # Cycle 1: C10 fails with 403, gets disabled
            rep1 = orch.execute("techpulse-daily-v1", slot=WHEN)
            assert orch.task_status(rep1.run_id, "C10") == "policy_blocked"
            assert orch.source_enabled("openalex") is False
            assert call_counts["C10"] == 1

            # Cycle 1.5: immediately within cooldown (30s later) -> not re-probed yet
            clock.advance(30)
            rep_half = orch.execute("techpulse-daily-v1", slot=clock.now())
            assert orch.source_enabled("openalex") is False
            assert orch.task_status(rep_half.run_id, "C10") == "policy_blocked"
            # C10 was NOT called (blocked without calling activity)
            assert call_counts["C10"] == 1

            # Cycle 2: after cooldown (60s more, total 90s later), probe succeeds!
            clock.advance(60)
            should_succeed["openalex"] = True

            rep2 = orch.execute("techpulse-daily-v1", slot=clock.now())
            assert call_counts["C10"] == 2  # probed!
            assert orch.source_enabled("openalex") is True  # re-enabled!
            assert orch.task_status(rep2.run_id, "C10") == "success"

            # Cycle 3: subsequent cycle runs normally as enabled
            clock.advance(60)
            rep3 = orch.execute("techpulse-daily-v1", slot=clock.now())
            assert call_counts["C10"] == 3
            assert orch.source_enabled("openalex") is True
            assert orch.task_status(rep3.run_id, "C10") == "success"


# ---------------------------------------------------------------------------
# 5. Wall-clock soak cadence after simulated suspend
# ---------------------------------------------------------------------------


class SuspendSimulatingClock:
    """Simulates a system clock where sleep during suspend advances wall-clock time."""

    def __init__(self, start_dt: datetime) -> None:
        self.current_time = start_dt
        self.sleep_slices: list[float] = []
        self.suspend_on_first_sleep = True
        self.suspend_duration_seconds = 14400.0  # 4 hours

    def now(self) -> datetime:
        return self.current_time

    def sleep(self, seconds: float) -> None:
        self.sleep_slices.append(seconds)
        if self.suspend_on_first_sleep:
            # First sleep slice: host machine suspends for 4 hours!
            self.suspend_on_first_sleep = False
            self.current_time += timedelta(seconds=self.suspend_duration_seconds)
        else:
            self.current_time += timedelta(seconds=seconds)


def test_wall_clock_scheduling_after_simulated_suspend() -> None:
    """Soak scheduling sleeps in short slices (<=30s). When host is suspended,

    it resumes immediately past due time, runs ONE cycle (no burst catch-up),
    and logs the actual gap.
    """
    sim_clock = SuspendSimulatingClock(WHEN)

    with TemporaryDirectory() as tmp:
        runner = SoakRunner(
            artifacts_dir=Path(tmp) / "artifacts",
            docs_dir=Path(tmp) / "docs",
            database_path=Path(tmp) / "soak.sqlite",
            clock=sim_clock,
            interval_minutes=60.0,
            fixture_mode=True,
        )

        next_due = WHEN + timedelta(minutes=60.0)

        # Runner sleeps in short slices. During the first slice, VM is suspended 4 hours.
        gap = runner.sleep_until_next_cycle(next_due, slice_seconds=30.0)

        # 1. The sleep slices were short (<= 30s)
        assert len(sim_clock.sleep_slices) > 0
        assert sim_clock.sleep_slices[0] <= 30.0

        # 2. Clock is now past next_due by approximately 3 hours (4h suspend - 1h interval)
        assert runner.clock.now() >= next_due
        assert gap >= 3.0 * 3600.0

        # 3. Exactly 1 sleep slice was needed because the resume detected now >= next_due immediately
        assert len(sim_clock.sleep_slices) == 1
