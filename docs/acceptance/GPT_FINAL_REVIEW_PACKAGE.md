# GPT Final-Review Package: TechPulse-AI (P0004)

> **SUPERSEDED / NOTE (2026-11-18):** This package was assembled for the GPT final
> review and is retained for history. Its V99 section is superseded by the final result
> (FAIL, 23/24; risk accepted and re-run waived 2026-11-18) and by the NO-GO verdict in
> `GPT_FINAL_REVIEW_VERDICT.md`. The AUTH-02 deploy target is corrected below to the
> **owner's Windows PC** (previously mis-stated as the dispatcher's Linux box); see
> `docs/decisions/auth02_deploy_target.md`.

Status: READY FOR GPT FINAL REVIEW, 2026-11-18 (London). master e91b881 (+ one docs-only commit adding docs/decisions/mvp_acceptance.md). **V99 24h soak = FAIL (23/24 cycles, 95.83% < 99%). The project owner accepted this single transient failure as a known risk on 2026-11-18. It is NOT a V99 pass and no threshold was changed.**

## 1. Summary
- TechPulse-AI is a local, self-hosted tech-intelligence pipeline. It runs collectors (openalex, crossref, arxiv, github, hn) through normalisation and retrieval (bilingual en/zh), then report rendering, through the O20 local orchestrator. It uses SQLite and a local file object store, and serves a FastAPI app on 127.0.0.1.
- It was built multi-agent: Antigravity, Grok, opencode free models and WorkBuddy models were the builders. Every ticket was reviewed read-only by a DIFFERENT model than its builder, with no self-review. Merges happened only after the dispatcher's own fully green suite run, done as a separate step. The full log is in .orchestra/JOURNAL.md.

## 2. Test results
- master e91b881 (all tickets merged, incl. SRCfix, SRCfix2, SRCfix3): **943 passed, 1 deselected (capacity marker), 1 warning** (PYTHONPATH=src, run on Cd's PC 2026-11-18, 123 s). Same count as the green run before the V99 start on 2026-11-17.
- ruff and mypy were clean per builder and reviewer reports at the time of each ticket (not re-run for this package).
## 3. Retrieval evaluation (honest, thresholds unchanged: nDCG@10 >= 0.75, Recall@20 >= 0.85)
| split | queries | nDCG@10 | Recall@20 | result |
|---|---|---|---|---|
| dev | 8 | 0.8347 | 0.9167 | pass |
| dev2 | 13 | 0.8572 | 1.0000 | pass |
| heldout | 11 | 0.8954 | **0.8182** | **Recall@20 FAIL** |
- Extended queries come from real corpus documents. glm-5.3-flash-free labelled them and big-pickle cross-checked them (79% row agreement and 97% label-pair agreement; disagreements resolved conservatively). Antigravity reviewed them: PASS.
- The held-out Recall@20 shortfall is recorded as a KNOWN LIMIT. It was deliberately NOT tuned, to avoid held-out leakage. The sealed TEST split was never accessed.

## 4. Decisions (delegated by Cd, 2026-10-29; recorded in docs/decisions)
- AUTH-02: local single-machine deploy on the owner's Windows PC. No Docker, Postgres or cloud spend. The image digest is "N/A (local deployment, no container)", not faked.
- AUTH-06: approved for a local/self-hosted "v1.0 local release" ONLY. No public outreach, no paid services. The production go/no-go stays refused for anything public.
- G20: offline parts only; outreach is off.
- M70: skipped. C30/C31: off.
- Gold queries: extended dev2/heldout set (see section 3). The held-out Recall@20 known limit is accepted without tuning: docs/decisions/heldout_recall_known_limit.md.

## 5. Deployment and V99
- Runtime for the V99 run: owner's Windows laptop "dcz666", Python 3.12 via uv, SQLite, API on 127.0.0.1:8000, scheduled tasks TP_V99_api / TP_V99_soak (now stopped and disabled). Earlier box runs v99box3 and v99box4 failed only because the Linux box suspends/restarts when idle (11 and 12 cycles, 0 source failures); the box is judged unable to run 24h unattended.
- **V99 result, run v99pc1** (2026-11-17T10:25:51Z to 2026-11-18T10:26:04Z, 24.00 h, hourly real O20 cycles):

| criterion | threshold | measured | verdict |
|---|---|---|---|
| duration | >= 24h | 24.00 h | pass |
| successful cycles | >= 23 | 23 of 24 | pass |
| max cycle gap | <= 7200 s | 3601 s | pass |
| P0 success rate | >= 99% | **95.83% (23/24)** | **FAIL** |
| duplicates written | 0 | 0 | pass |
| cost | <= $5 | $0.00 | pass |
| security events | 0 | 0 leaks, 0 escalations | pass |

- Per source: openalex 24/24, crossref 24/24, github 24/24, hn 24/24, arxiv 23/24.
- Cause of the only failure: cycle 21 (v99-20261118T062551Z-0021, 2026-11-18 06:25:51Z): arxiv (C12) returned `retryable_error`; the other 4 sources succeeded and QA passed. With 24 cycles per day, any single failed cycle puts the rate below 99%.
- **Recorded honestly as: V99 FAIL (23/24, one transient arxiv retryable_error); owner accepted the risk on 2026-11-18.** soak_test.json status is FAIL; docs/decisions/mvp_acceptance.md says REFUSED (FAIL) and carries the owner-acceptance addendum.
- Process finding: an earlier read suggested the soak process was stuck after 24 h (no result file, heartbeat stopped 09:40:14Z). That was wrong: the soak finished normally at 10:26:04Z and wrote soak_test.json, manifest.json, metrics.json and mvp_acceptance.md; heartbeat.json is only written after each cycle. The earlier check failed because the PC shell was extremely slow, not the soak. No soak process was left running.
- Observation (not investigated): cycle duration rose from about 65 s by day to about 900-1500 s overnight.
- Evidence (local, git-ignored): artifacts/v99pc1_owner_accepted/ (cycles.jsonl, soak.log, heartbeat.json, soak_state.json, manifest.json, metrics.json, soak_test.json, status output).

## 6. Known limits
1. V99 FAILED the 99% success threshold (23/24); accepted by the owner as a known risk, not a pass (section 5).
2. Held-out Recall@20 is 0.8182, below 0.85 (not tuned; see section 3; measured at d75bbe5, not re-measured here). The dev split has only 8 queries; all splits are small (32 queries in total).
3. The cause of the v99box3 duplicate written after resume (1 duplicate, box run) is not investigated. v99pc1 had 0 duplicates.
4. Single machine with SQLite; no HA or backups beyond scripts/backup.sh. The 24h run needs the PC to stay awake and logged in.
5. LLM features run with a $0 budget (the WorkBuddy relay is reachable only from Cd's PC).
6. Public release, outreach, fine-tuning and third-party writes are all disabled by decision.
7. Review coverage relied on free or cheap models (Copilot and Grok quotas were exhausted mid-project).

## 7. Delivery (only AFTER GPT final-review approval)
- Push to GitHub dcz66614 (own repo), and upload to Drive jalenbrunson6666 'agent project auto/P0004-techpulse-ai'. Not done yet.

---

Note (2026-11-18): this package was assembled for the final review. Its V99 section
(section 5) is **superseded** by the final result recorded in
`docs/decisions/mvp_acceptance.md` and in this archive. The final V99 verdict is
**FAIL** (23/24 cycles, 95.83% < 99%), owner-accepted with an explicit re-run waiver
on 2026-11-18. It is not a V99 pass. See `GPT_FINAL_REVIEW_VERDICT.md`.
