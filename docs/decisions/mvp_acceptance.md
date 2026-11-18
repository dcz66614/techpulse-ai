# Decision: MVP Acceptance and V99 24-Hour Soak Test Sign-Off

- **Status:** **REFUSED (FAIL)**
- **Run ID:** `v99pc1`
- **Evaluation Timestamp:** `2026-11-18T10:26:04Z`
- **Test Start Timestamp:** `2026-11-17T10:25:51Z`
- **Deploy Target:** Owner's Windows laptop "dcz666" (Python 3.12 via uv, SQLite, no Docker, no Postgres), master e91b881
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
- **Deploy Target (corrected 2026-11-18):** an earlier generator said "dedicated Linux box", but the separate Linux target was never validated. Run v99pc1 actually ran on the owner's Windows PC after two box runs (v99box3, v99box4) failed from box suspension. AUTH-02 was corrected to the owner's PC by owner decision 2026-11-18.
- **Published Reports:** 24 reports published with full provenance.

---

## 5. Owner Acceptance Addendum (2026-11-18)

- **V99 verdict is unchanged: FAIL.** 23/24 successful cycles = 95.83%, below the 99.0% threshold. No threshold was relaxed and this is not a PASS.
- **Cause:** cycle 21 (`v99-20261118T062551Z-0021`, 2026-11-18 06:25:51Z) failed because arxiv (C12) returned `retryable_error`. The other four sources were 100% in that cycle and QA passed. Over 24 hourly cycles, a single failed cycle puts success at 95.83%, so one transient source error makes the 99% bar unreachable.
- **Everything else met its bar:** duration 24.00h, max gap 3601s (limit 7200s), 0 duplicates, $0.00 cost, 0 security events.
- **Owner decision (Cd, 2026-11-18):** accept this single transient failure as a known limit and proceed to the GPT final review. This is a recorded risk acceptance, not a V99 pass.
- **Re-run explicitly WAIVED (Cd, 2026-11-18):** the owner waived a re-run of the 24h V99 soak. No re-run was performed and none is planned. The formal verdict stays **FAIL**; the 99% threshold was not relaxed.
- **Observation:** cycle duration rose from about 65s (daytime) to about 900-1500s overnight on the owner's PC. All completed, but the cause (PC load or source latency) was not investigated.
- Evidence: archived under `docs/acceptance/evidence/v99pc1/` (previously `artifacts/v99pc1_owner_accepted/`, local only, git-ignored).
