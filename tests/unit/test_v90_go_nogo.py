"""Unit tests for Ticket V90 staging go/no-go gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from techpulse.release.go_nogo import (
    CheckStatus,
    evaluate_go_nogo,
    repository_root,
    run_go_nogo,
    write_release_artifacts,
)

ROOT = repository_root()


def test_v90_pass_case(tmp_path: Path) -> None:
    """Verifies that the current committed codebase passes staging go/no-go."""
    artifacts_dir = tmp_path / "artifacts"
    result = evaluate_go_nogo(root=ROOT, artifacts_dir=artifacts_dir, auth06_granted=False)

    assert result.ticket == "V90"
    assert result.release_status == "staging READY"
    assert result.staging_allowed is True
    assert result.production_allowed is False
    assert result.auth06_granted is False
    assert len(result.blockers) == 0

    # Verify the 6 gated items are present
    assert len(result.gated_items) == 6
    gated_codes = {g.code for g in result.gated_items}
    assert gated_codes == {"AUTH-02", "AUTH-06", "V99", "M70", "C30_C31", "G20"}

    # Verify Section 10 thresholds: 13 PASS, 1 GATED (V99 soak)
    passed_thresholds = [c for c in result.section_10_thresholds if c.status == CheckStatus.PASS]
    gated_thresholds = [c for c in result.section_10_thresholds if c.status == CheckStatus.GATED]
    failed_thresholds = [c for c in result.section_10_thresholds if c.status == CheckStatus.FAIL]

    assert len(passed_thresholds) == 13
    assert len(gated_thresholds) == 1
    assert len(failed_thresholds) == 0
    assert gated_thresholds[0].responsible_task == "V99"

    # Verify all 29 prior P0 tasks are PASS, V99 is GATED
    passed_p0 = [p for p in result.p0_tasks if p.status == CheckStatus.PASS]
    gated_p0 = [p for p in result.p0_tasks if p.status == CheckStatus.GATED]
    failed_p0 = [p for p in result.p0_tasks if p.status == CheckStatus.FAIL]

    assert len(passed_p0) == 29
    assert len(gated_p0) == 1
    assert len(failed_p0) == 0
    assert gated_p0[0].task_id == "V99"

    # Verify security, permissions, price snapshot, and data sources
    assert result.permissions_and_egress["status"] == "PASS"
    assert result.permissions_and_egress["policy_default"] == "deny"
    assert result.price_snapshot["status"] == "PASS"
    assert result.price_snapshot["paid_models_count"] == 0
    assert result.lawful_data_sources["status"] == "PASS"
    assert "Cd (AUTH-04)" in result.lawful_data_sources["approval_authority"]
    assert result.security_signoff["status"] == "PASS"
    assert result.security_signoff["secret_leak_events"] == 0
    assert result.security_signoff["privilege_escalation_events"] == 0

    # Test writing artifacts
    written = write_release_artifacts(result, artifacts_dir, tmp_path)
    assert written["go_nogo"].is_file()
    assert written["metrics"].is_file()
    assert written["manifest"].is_file()
    assert written["release_acceptance"].is_file()

    # Verify manifest integrity
    manifest = json.loads(written["manifest"].read_text(encoding="utf-8"))
    assert manifest["status"] == "staging READY"
    assert manifest["ticket"] == "V90"
    for item in manifest["artifacts"]:
        name = Path(item["path"]).name
        target_file = written["go_nogo"].parent / name
        assert target_file.is_file()
        assert hashlib.sha256(target_file.read_bytes()).hexdigest() == item["sha256"]

    # Verify release_acceptance.md contents
    markdown = written["release_acceptance"].read_text(encoding="utf-8")
    assert "staging READY" in markdown
    assert "AUTH-02 deploy pending" in markdown
    assert "AUTH-06 production sign-off pending" in markdown
    assert "V99 24h soak not run" in markdown
    assert "M70 optional skipped_not_applicable" in markdown
    assert "C30/C31 off" in markdown
    assert "G20 needs AUTH-06" in markdown


def test_v90_failing_thresholds() -> None:
    """Verifies that any failing threshold or security violation blocks release."""
    # 1. Dedup precision fails
    res_dedup = evaluate_go_nogo(root=ROOT, override_thresholds={"dedup_precision": 0.90})
    assert res_dedup.release_status == "BLOCKED"
    assert res_dedup.staging_allowed is False
    assert res_dedup.production_allowed is False
    assert any("Precision / Recall" in b for b in res_dedup.blockers)

    # 2. Dedup recall fails
    res_rec = evaluate_go_nogo(root=ROOT, override_thresholds={"dedup_recall": 0.80})
    assert res_rec.release_status == "BLOCKED"
    assert any("Precision / Recall" in b for b in res_rec.blockers)

    # 3. Content extraction fails
    res_ext = evaluate_go_nogo(root=ROOT, override_thresholds={"content_extraction_rate": 0.90})
    assert res_ext.release_status == "BLOCKED"
    assert any("正文抽取成功率" in b for b in res_ext.blockers)

    # 4. Schema completeness fails
    res_sch = evaluate_go_nogo(root=ROOT, override_thresholds={"schema_completeness_rate": 0.90})
    assert res_sch.release_status == "BLOCKED"
    assert any("必填字段完整率" in b for b in res_sch.blockers)

    # 5. Retrieval nDCG fails
    res_ndcg = evaluate_go_nogo(root=ROOT, override_thresholds={"retrieval_ndcg_at_10": 0.70})
    assert res_ndcg.release_status == "BLOCKED"
    assert any("nDCG@10" in b for b in res_ndcg.blockers)

    # 6. Retrieval Recall fails
    res_rec20 = evaluate_go_nogo(root=ROOT, override_thresholds={"retrieval_recall_at_20": 0.80})
    assert res_rec20.release_status == "BLOCKED"
    assert any("Recall@20" in b for b in res_rec20.blockers)

    # 7. Claim citation coverage fails
    res_cite = evaluate_go_nogo(root=ROOT, override_thresholds={"claim_citation_coverage": 0.90})
    assert res_cite.release_status == "BLOCKED"
    assert any("Claim 引用覆盖" in b for b in res_cite.blockers)

    # 8. URL traceability fails
    res_url = evaluate_go_nogo(root=ROOT, override_thresholds={"url_traceability": 0.98})
    assert res_url.release_status == "BLOCKED"
    assert any("URL 可追溯" in b for b in res_url.blockers)

    # 9. Numeric consistency fails
    res_num = evaluate_go_nogo(root=ROOT, override_thresholds={"numeric_date_consistency": 0.90})
    assert res_num.release_status == "BLOCKED"
    assert any("日期/数值一致" in b for b in res_num.blockers)

    # 10. Red team fixtures count fails
    res_fix = evaluate_go_nogo(root=ROOT, override_thresholds={"redteam_fixtures": 50})
    assert res_fix.release_status == "BLOCKED"
    assert any("红队 fixtures" in b for b in res_fix.blockers)

    # 11. Secret leaks > 0
    res_sec = evaluate_go_nogo(root=ROOT, override_thresholds={"secret_leaks": 1})
    assert res_sec.release_status == "BLOCKED"
    assert any("Secret 泄漏" in b or "Security sign-off failed" in b for b in res_sec.blockers)

    # 12. Unauthorized tool calls > 0
    res_tool = evaluate_go_nogo(root=ROOT, override_thresholds={"unauthorized_tool_calls": 1})
    assert res_tool.release_status == "BLOCKED"
    assert any("Tool Call" in b or "Permissions and Egress" in b for b in res_tool.blockers)

    # 13. Egress violations > 0
    res_egress = evaluate_go_nogo(root=ROOT, override_thresholds={"egress_violations": 1})
    assert res_egress.release_status == "BLOCKED"
    assert any("Egress 越权" in b or "Permissions and Egress" in b for b in res_egress.blockers)

    # 14. Daily LLM spend budget exceeded (> $25)
    res_spend = evaluate_go_nogo(root=ROOT, override_thresholds={"daily_llm_spend_usd": 30.0})
    assert res_spend.release_status == "BLOCKED"
    assert any(
        "LLM 总支出" in b or "Pricing snapshot check failed" in b for b in res_spend.blockers
    )

    # 15. Unapproved data source enabled (sec_edgar enabled without AUTH-04)
    res_source = evaluate_go_nogo(root=ROOT, override_thresholds={"sec_edgar_enabled": True})
    assert res_source.release_status == "BLOCKED"
    assert any("Data source check failed" in b for b in res_source.blockers)

    # 16. Policy default allow
    res_policy = evaluate_go_nogo(root=ROOT, override_thresholds={"policy_default": "allow"})
    assert res_policy.release_status == "BLOCKED"
    assert any("Permissions and Egress" in b for b in res_policy.blockers)

    # 17. Failing P0 task
    res_p0 = evaluate_go_nogo(root=ROOT, override_thresholds={"failed_p0_tasks": ["C10"]})
    assert res_p0.release_status == "BLOCKED"
    assert any("P0 Task [C10]" in b for b in res_p0.blockers)


def test_v90_auth06_cap_staging_only() -> None:
    """Verifies that absent AUTH-06 strictly caps result at staging-only."""
    # When AUTH-06 is absent: staging READY
    result_staging = evaluate_go_nogo(root=ROOT, auth06_granted=False)
    assert result_staging.release_status == "staging READY"
    assert result_staging.staging_allowed is True
    assert result_staging.production_allowed is False
    assert result_staging.auth06_granted is False

    # When AUTH-06 is signed: production READY
    result_prod = evaluate_go_nogo(root=ROOT, auth06_granted=True)
    assert result_prod.release_status == "production READY"
    assert result_prod.staging_allowed is True
    assert result_prod.production_allowed is True
    assert result_prod.auth06_granted is True

    # Even with AUTH-06 granted, failing thresholds MUST block release
    result_blocked = evaluate_go_nogo(
        root=ROOT,
        auth06_granted=True,
        override_thresholds={"secret_leaks": 1},
    )
    assert result_blocked.release_status == "BLOCKED"
    assert result_blocked.staging_allowed is False
    assert result_blocked.production_allowed is False


def test_v90_run_go_nogo_persists_expected_files(tmp_path: Path) -> None:
    """Verifies that run_go_nogo writes all expected artifacts and decision docs."""
    run_id = "v90-test-run"
    artifacts_dir = tmp_path / "artifacts"
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    # Copy needed config files to test repo
    (repo_root / "config").mkdir(parents=True, exist_ok=True)
    (repo_root / "config" / "policy.yaml").write_text(
        (ROOT / "config" / "policy.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo_root / "config" / "source_registry.yaml").write_text(
        (ROOT / "config" / "source_registry.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo_root / "config" / "model_router.yaml").write_text(
        (ROOT / "config" / "model_router.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo_root / "config" / "deploy.yaml").write_text(
        (ROOT / "config" / "deploy.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo_root / "data" / "gold").mkdir(parents=True, exist_ok=True)
    (repo_root / "data" / "gold" / "dedup_gold.jsonl").write_text(
        (ROOT / "data" / "gold" / "dedup_gold.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
    )

    result = run_go_nogo(
        run_id=run_id,
        artifacts_dir=artifacts_dir,
        repo_root=repo_root,
        auth06_granted=False,
    )

    assert result.release_status == "staging READY"
    assert (artifacts_dir / run_id / "V90" / "go_nogo.json").is_file()
    assert (artifacts_dir / run_id / "V90" / "metrics.json").is_file()
    assert (artifacts_dir / run_id / "V90" / "manifest.json").is_file()
    assert (repo_root / "docs" / "decisions" / "release_acceptance.md").is_file()
