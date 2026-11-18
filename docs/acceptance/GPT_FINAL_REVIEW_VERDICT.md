# GPT Final-Review Verdict

- **Verdict:** NO-GO
- **Date:** 2026-11-18
- **Scope:** TechPulse-AI / 知潮, P0004, latest code `f41d756`
- **Note:** This is NOT a final-acceptance-passed release. It is a development build
  with documented known issues, released locally/self-hosted only.

The final review returned NO-GO for five reasons. Each is resolved below as `fixed` or `owner-waived`.

| # | Reason for NO-GO | Resolution | Where recorded |
|---|---|---|---|
| 1 | V99 24h soak FAIL: 23/24 cycles (95.83% < 99%). Risk accepted, not a PASS. | owner-waived | `docs/decisions/mvp_acceptance.md` — Cd accepted the risk and explicitly waived a re-run on 2026-11-18; verdict stays FAIL, no threshold relaxed. |
| 2 | Held-out Recall@20 0.8182 < 0.85 and not re-measured on latest code. | owner-waived | `docs/decisions/heldout_recall_known_limit.md` — re-measured once, sealed, on `f41d756`: nDCG@10 0.8954 (ok), Recall@20 0.8182 (below). RETfix found no sound improvement; no src change. Status owner-waived. |
| 3 | v99box3 duplicate-after-resume root cause unknown (v99pc1 had 0 duplicates, but that does not prove the resume scenario). | fixed (regression-tested; no new 24h run, re-run owner-waived) | Root cause: `SoakRunner` kept one in-memory record list per run_id; when a collector timed out after the 6.5h host suspension and was retried, records of both attempts were archived by D10, so the same `(source_id, source_record_id)` (github 1362490) appeared twice and the orchestrator counted 1 duplicate. Fix (`src/techpulse/ops/soak.py`): per-task record buffer with an attempt guard, a successful retry fully replaces earlier attempts, D10 dedupe on `(source_id, source_record_id)`, per-task records persisted to disk for restart/adopt recovery, saved `last_due` used for the next slot. 950 tests incl. new regression tests; not re-validated by a new 24h run (waived). Tests: `tests/integration/test_dupfix_regressions.py`. Builder: hy3 (last step: free opencode model); reviewer: Copilot (PASS). |
| 4 | AUTH-02 deploy target in docs was a separate Linux machine, but the validated environment (smoke + 24h v99pc1) was the owner's Windows PC. | fixed | `docs/decisions/auth02_deploy_target.md` — new Current Decision dated 2026-11-18 sets the target to the Windows PC; the 2026-10-29 Linux entry is in Decision History as superseded. `docs/deploy.md`, `docs/decisions/release_acceptance.md`, config comments and runner comments updated. |
| 5 | README still said P00 scaffold stage; `.orchestra/` and `artifacts/` are git-ignored, so pushing would not carry the evidence. | fixed | `README.md` rewritten to the real state; evidence archived in tracked `docs/acceptance/` (including `evidence/`). |

## Evidence

- Original review package: `GPT_FINAL_REVIEW_PACKAGE.md` (its V99 section is superseded
  by the final result above).
- Archived soak/eval evidence: `evidence/` (`v99pc1/` and `split_eval_f41d756.txt`).
  v99box3/box4 raw evidence was not archived in the repo (artifacts are git-ignored); the DUP root cause analysis is recorded above.
