"""O30 metrics, budget breaker, and alerts.

Tests use tempfile. They do not read an env file, call a provider, or touch
the sealed retrieval set.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from techpulse.contracts import TaskStatus
from techpulse.ops.alerts import SOP_LINKS, raise_alert
from techpulse.ops.metrics import (
    DAILY_CAP_USD,
    DEGRADE_USD,
    STOP_USD,
    BudgeterLocked,
    MetricsStore,
    RouterRejected,
    Usage,
    load_model_router,
    publish_o30,
    repository_root,
    route_model,
)
from techpulse.workflows.activities import ActivityContext, ActivityResult
from techpulse.workflows.orchestrator import (
    DAILY_MAX_USD,
    LocalOrchestrator,
    ManualClock,
    pipeline_task_ids,
)

_SLOT = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
_DAY = "2026-10-28"
_GITHUB = "ghp_" + ("A" * 36)
_OPENAI = "sk-" + ("b" * 24)
_LABELED = "token=super-secret-value"
_ROOT = repository_root()


def _usage(
    *,
    task_id: str = "C10",
    source_id: str = "openalex",
    customer_id: str = "internal",
    agent_role: str = "CollectorAgent",
    input_tokens: int = 10,
    output_tokens: int = 4,
    cost_usd: float = 1.0,
    latency_ms: int = 100,
    data_volume: int = 1000,
    qa_pass: bool | None = True,
    anomaly_blocked: bool = False,
) -> Usage:
    return Usage(
        run_id="run-o30",
        task_id=task_id,
        source_id=source_id,
        model_id="local/template",
        customer_id=customer_id,
        agent_role=agent_role,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        data_volume=data_volume,
        qa_pass=qa_pass,
        anomaly_blocked=anomaly_blocked,
        pricing_snapshot_id="none",
    )


def _activity(context: ActivityContext) -> ActivityResult:
    flag = True if context.task_id == "Q10" else None
    return ActivityResult(
        status=TaskStatus.SUCCESS,
        records=(context.task_id,),
        qa_pass=flag,
        output_sha256=context.input_sha256,
    )


def _open(root: Path, clock: ManualClock) -> LocalOrchestrator:
    activities = {task_id: _activity for task_id in pipeline_task_ids()}
    return LocalOrchestrator(root / "o20.sqlite", clock=clock, activities=activities)


def test_cap_constants_match_the_daily_limit() -> None:
    """The O20 pause and the O30 breaker share the $25 cap. Degrade is $20."""
    assert DAILY_CAP_USD == 25
    assert DEGRADE_USD == 20
    assert STOP_USD == 25
    assert DAILY_MAX_USD == DAILY_CAP_USD


def test_rollups_group_tokens_cost_latency_volume_qa_and_blocks() -> None:
    """Usage is summed by source, task, model, and customer."""
    with TemporaryDirectory() as tmp:
        with MetricsStore(Path(tmp) / "o30.sqlite") as store:
            store.record(_usage(), day=_DAY)
            store.record(
                _usage(
                    task_id="C20",
                    source_id="github",
                    customer_id="cust-a",
                    input_tokens=3,
                    output_tokens=1,
                    cost_usd=2.0,
                    latency_ms=50,
                    data_volume=20,
                    qa_pass=False,
                    anomaly_blocked=True,
                ),
                day=_DAY,
            )
            store.record(
                _usage(
                    task_id="R10",
                    source_id="openalex",
                    customer_id="cust-a",
                    agent_role="WriterAgent",
                    input_tokens=7,
                    output_tokens=2,
                    cost_usd=0.0,
                    latency_ms=10,
                    data_volume=5,
                    qa_pass=None,
                ),
                day=_DAY,
            )
            by_source = {item.key: item for item in store.rollup("source")}
            by_task = {item.key: item for item in store.rollup("task")}
            by_model = {item.key: item for item in store.rollup("model")}
            by_customer = {item.key: item for item in store.rollup("customer")}
            openalex = by_source["openalex"]
            assert openalex.input_tokens == 17
            assert openalex.output_tokens == 6
            assert openalex.cost_usd == 1.0
            assert openalex.latency_ms == 110
            assert openalex.data_volume == 1005
            assert openalex.qa_pass == 1
            assert openalex.qa_fail == 0
            assert openalex.anomaly_blocked == 0
            github = by_source["github"]
            assert github.qa_pass == 0
            assert github.qa_fail == 1
            assert github.anomaly_blocked == 1
            assert github.cost_usd == 2.0
            assert set(by_task) == {"C10", "C20", "R10"}
            assert by_model["local/template"].samples == 3
            assert by_model["local/template"].input_tokens == 20
            assert by_customer["internal"].samples == 1
            assert by_customer["cust-a"].samples == 2
            assert store.decision(day=_DAY).spent_usd == 3.0


def test_simulated_spend_degrades_at_20_and_stops_at_25() -> None:
    """$20 degrades. $25 opens the circuit breaker. $19.99 does neither."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with MetricsStore(root / "below.sqlite") as below:
            quiet = below.inject_simulated_spend(19.99, day=_DAY)
            assert quiet.mode == "normal"
            assert quiet.circuit_open is False
        with MetricsStore(root / "degrade.sqlite") as degrade:
            decision = degrade.inject_simulated_spend(20, day=_DAY)
            assert decision.mode == "degraded"
            assert decision.circuit_open is False
            assert decision.spent_usd == 20
            assert decision.budgeter_locked is True
            blocked = decision.allow(priority="P1", uses_llm=True, tier=2, essential=False)
            assert blocked is False
            assert (
                route_model(decision, priority="P1", uses_llm=True, tier=2, essential=False) is None
            )
            kept = decision.allow(priority="P0", uses_llm=True, tier=2, essential=True)
            assert kept is True
            assert route_model(decision, priority="P0", uses_llm=True, tier=0) == "local/template"
        with MetricsStore(root / "stop.sqlite") as stop:
            stopped = stop.inject_simulated_spend(25, day=_DAY)
            assert stopped.mode == "stopped"
            assert stopped.circuit_open is True
            assert stopped.allow(priority="P1", uses_llm=True, tier=1) is False
            assert stopped.allow(priority="P0", uses_llm=True, tier=1) is True
            assert stopped.allow(priority="P1", uses_llm=False) is True


def test_metrics_unavailable_pauses_non_p0_llm() -> None:
    """A metrics outage fail-closes non-P0 LLM calls and leaves the budgeter locked."""
    with TemporaryDirectory() as tmp:
        with MetricsStore(Path(tmp) / "o30.sqlite") as store:
            store.set_metrics_available(False)
            decision = store.decision(day=_DAY)
            assert decision.mode == "fail_closed"
            assert decision.circuit_open is False
            assert decision.budgeter_locked is True
            assert decision.allow(priority="P1", uses_llm=True, tier=1) is False
            assert decision.allow(priority="P0", uses_llm=True, tier=1) is True
            assert decision.allow(priority="P1", uses_llm=False) is True


def test_budgeter_cannot_be_disabled() -> None:
    """A false flag, a false config block, and a SQL update all leave the budgeter on."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with pytest.raises(BudgeterLocked):
            MetricsStore(root / "skip.sqlite", budgeter_enabled=False)
        text = (_ROOT / "config" / "model_router.yaml").read_text(encoding="utf-8")
        disabled = root / "disabled.yaml"
        disabled.write_text(text.replace("enabled: true", "enabled: false", 1), encoding="utf-8")
        with pytest.raises(BudgeterLocked):
            load_model_router(disabled)
        raised = root / "cap.yaml"
        raised.write_text(
            text.replace("daily_cap_usd: 25", "daily_cap_usd: 100", 1), encoding="utf-8"
        )
        with pytest.raises(RouterRejected):
            load_model_router(raised)
        with MetricsStore(root / "o30.sqlite") as store:
            with pytest.raises(sqlite3.IntegrityError):
                store.connection.execute("UPDATE o30_control SET budgeter_locked = 0 WHERE id = 1")
            row = store.connection.execute(
                "SELECT budgeter_locked FROM o30_control WHERE id = 1"
            ).fetchone()
            assert row is not None
            assert row["budgeter_locked"] == 1
            assert store.decision(day=_DAY).budgeter_locked is True


def test_router_lists_only_the_local_template() -> None:
    """The committed router has one model and no paid official API."""
    text = (_ROOT / "config" / "model_router.yaml").read_text(encoding="utf-8")
    lowered = text.lower()
    for name in ("openai", "anthropic", "gemini", "claude", "azure", "bedrock"):
        assert name not in lowered
    router = load_model_router()
    assert router.default_model == "local/template"
    assert router.models == ("local/template",)
    assert router.daily_cap_usd == 25
    with TemporaryDirectory() as tmp:
        paid = Path(tmp) / "paid.yaml"
        paid.write_text(text.replace("id: local/template", "id: openai/gpt-4", 1), encoding="utf-8")
        with pytest.raises(RouterRejected):
            load_model_router(paid)


def test_alerts_carry_run_id_and_an_sop_link() -> None:
    """Each alert kind stores the run id and a heading in docs/ops_slo.md."""
    sop = (_ROOT / "docs" / "ops_slo.md").read_text(encoding="utf-8")
    headings = {
        "p0-failure": "## P0 failure",
        "source-policy-change": "## Source policy change",
        "stale-evidence": "## Stale evidence",
    }
    with TemporaryDirectory() as tmp:
        with MetricsStore(Path(tmp) / "o30.sqlite") as store:
            for kind, link in SOP_LINKS.items():
                alert = raise_alert(
                    store.connection,
                    kind=kind,
                    run_id="run-o30",
                    summary=f"check {kind}",
                )
                fragment = link.split("#", 1)[1]
                assert alert.run_id == "run-o30"
                assert alert.sop == link
                assert link.startswith("docs/ops_slo.md#")
                assert headings[fragment] in sop
            with pytest.raises(ValueError):
                raise_alert(store.connection, kind="other", run_id="run-o30", summary="x")
            with pytest.raises(ValueError):
                raise_alert(store.connection, kind="p0_failure", run_id="", summary="x")


def test_token_and_secret_values_are_not_logged() -> None:
    """Credential values are dropped or redacted. Token counts stay numeric."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with MetricsStore(root / "o30.sqlite") as store:
            store.record(_usage(input_tokens=11, output_tokens=4), day=_DAY)
            store.log_event(
                {
                    "api_key": _GITHUB,
                    "event": "note",
                    "message": f"{_GITHUB} {_OPENAI} {_LABELED}",
                }
            )
            with pytest.raises(ValueError) as caught:
                store.record(_usage(customer_id=_GITHUB), day=_DAY)
            assert _GITHUB not in str(caught.value)
            raise_alert(
                store.connection,
                kind="p0_failure",
                run_id="run-o30",
                summary=f"saw {_OPENAI}",
            )
            text = store.stored_text()
            log_path = root / "log.jsonl"
            log_path.write_text(store.log_text() + "\n", encoding="utf-8")
            written = log_path.read_text(encoding="utf-8")
            for secret in (_GITHUB, _OPENAI, "super-secret-value"):
                assert secret not in text
                assert secret not in written
                assert secret not in str(caught.value)
            assert '"input_tokens":11' in written
            assert '"output_tokens":4' in written


def test_orchestrator_hook_degrades_then_stops() -> None:
    """$20 keeps the run in degrade mode. $25 pauses with the daily-budget reason."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        clock = ManualClock(_SLOT)
        with _open(root / "degrade", clock) as orch:
            orch.note_spend(20)
            assert orch.budget_mode() == "degraded"
            allowed = orch.llm_allowed(priority="P1", uses_llm=True, tier=2, essential=False)
            assert allowed is False
            report = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert report.activity_calls > 0
            assert report.status == "success"
            assert orch.pause_reason() == ""
        clock = ManualClock(_SLOT)
        with _open(root / "stop", clock) as orch:
            orch.note_spend(25)
            assert orch.budget_mode() == "stopped"
            stopped = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert stopped.activity_calls == 0
            assert stopped.status == "paused"
            assert orch.pause_reason() == "daily budget"


def test_orchestrator_fail_closed_still_runs_ingestion() -> None:
    """When metrics are down, non-P0 LLM calls pause and the daily chain still runs."""
    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock) as orch:
            orch.set_metrics_available(False)
            assert orch.budget_mode() == "fail_closed"
            assert orch.llm_allowed(priority="P1", uses_llm=True, tier=1) is False
            assert orch.llm_allowed(priority="P0", uses_llm=True, tier=1) is True
            assert orch.llm_allowed(priority="P1", uses_llm=False) is True
            report = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert report.activity_calls > 0
            assert report.status == "success"


def test_publish_writes_manifest_and_metrics() -> None:
    """The local fixture writes the O30 manifest, metrics, and log."""
    with TemporaryDirectory() as tmp:
        directory = publish_o30(Path(tmp), "o30-fixture")
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
        log_text = (directory / "log.jsonl").read_text(encoding="utf-8")
        assert manifest["status"] == "success"
        assert manifest["ticket"] == "O30"
        assert manifest["run_id"] == "o30-fixture"
        assert manifest["paid_models"] == []
        assert metrics["models"] == ["local/template"]
        assert metrics["paid_apis"] == []
        assert metrics["daily_cap_usd"] == 25
        assert metrics["probes"][0]["mode"] == "degraded"
        assert metrics["probes"][0]["circuit_open"] is False
        assert metrics["probes"][1]["mode"] == "stopped"
        assert metrics["probes"][1]["circuit_open"] is True
        assert metrics["probes"][1]["allow_non_p0_llm"] is False
        assert metrics["probes"][2]["mode"] == "fail_closed"
        assert metrics["rollups"]["model"][0]["input_tokens"] == 120
        kinds = {item["kind"] for item in manifest["alerts"]}
        assert kinds == {"p0_failure", "source_policy_change", "stale_evidence"}
        for item in manifest["alerts"]:
            assert item["run_id"] == "o30-fixture"
            assert item["sop"].startswith("docs/ops_slo.md#")
        for entry in manifest["files"]:
            name = Path(str(entry["path"])).name
            digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
            assert entry["sha256"] == digest
        assert _GITHUB not in log_text
        assert "o30-fixture" in log_text
