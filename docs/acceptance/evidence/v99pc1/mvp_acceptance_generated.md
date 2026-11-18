# Decision: MVP Acceptance and V99 24-Hour Soak Test Sign-Off

- **Status:** **REFUSED (FAIL)**
- **Run ID:** `v99pc1`
- **Evaluation Timestamp:** `2026-11-18T10:26:04Z`
- **Test Start Timestamp:** `2026-11-17T10:25:51Z`
- **Deploy Target:** Dedicated always-on Linux box (x86_64, Python via uv, SQLite, no Docker, no Postgres)
- **Authority:** OpsAgent / Dispatcher under AUTH-02 / AUTH-06 (Ticket DECbox)

---

## 1. Executive Summary

The 24-hour soak test evaluation determined status **FAIL**. Measured elapsed time is 24.00 hours (required: ≥24.00h) with 23/24 successful cycles (95.83%). Blockers encountered:
- P0 success rate 95.83% below threshold (99.0%).

---

## 2. V99 Acceptance Criteria vs Measured Performance

| Criterion | Threshold | Measured Result | Verdict |
|---|---|---|---|
| Unattended Soak Duration | ≥24.0 hours | 24.00h (86413s) | PASS |
| 24h P0 Execution Success Rate | ≥99.0% | 95.83% (23/24) | FAIL |
| Hourly Cadence & Gap Control | ≥23 cycles, gap ≤2.0h | 23 cycles, max gap 1.00h | PASS |
| Duplicate Records Written | 0 duplicates | 0 duplicates | PASS |
| Soak Resource Budget Spend | ≤$5.00 USD | $0.00 USD | PASS |
| Security / Secret Violations | 0 events | 0 leaks, 0 escalations | PASS |
| Lawful Sources (AUTH-04) | 100% compliant | Core 5 sources verified (openalex, crossref, arxiv, github, hn) | PASS |

---

## 3. Per-Source Reliability Breakdown

| Source ID | Target Family | Cycles Run | Successful Cycles | Success Rate |
|---|---|---|---|---|
| `openalex` | AUTH-04 Approved | 24 | 24 | 100.0% |
| `crossref` | AUTH-04 Approved | 24 | 24 | 100.0% |
| `arxiv` | AUTH-04 Approved | 24 | 23 | 95.8% |
| `github` | AUTH-04 Approved | 24 | 24 | 100.0% |
| `hn` | AUTH-04 Approved | 24 | 24 | 100.0% |

---

## 4. Operational Invariants and Deployment Verification

- **Idempotency & Safe Resume:** Checkpointed state verified via SQLite (`o20_soak.sqlite`).
- **Failures Recorded Honestly:** Network failures are logged with full run_id lineage and never faked.
- **Dedicated Linux Target:** Deploy target runs on dedicated Linux machine (x86_64); owner's PC was superseded due to nightly shutdowns.
- **Published Reports:** 24 reports published with full provenance.

