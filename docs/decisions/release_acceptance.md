# Decision: TechPulse-AI Staging Release Acceptance and Security Sign-off (V90)

> **SUPERSEDED (2026-11-18):** The production/release sign-off below predates the V99
> FAIL (23/24 cycles, 95.83% < 99%; risk owner-accepted, re-run explicitly waived
> 2026-11-18), the GPT final review **NO-GO** (2026-11-18), and the AUTH-02 deploy-target
> correction (owner's Windows PC, not a separate Linux machine). It is retained only as
> historical V90-stage evidence. **This project is a development build with documented
> known limitations, NOT a passed final acceptance.** Statements of "production READY",
> "APPROVED", "PASS" for the release gate, and "V99 pending/GATED" below are superseded.
> See `docs/acceptance/GPT_FINAL_REVIEW_VERDICT.md`, `mvp_acceptance.md`,
> `heldout_recall_known_limit.md` and `auth02_deploy_target.md`.

- **Ticket:** `V90`
- **Run ID:** `v90-20261029T120754Z`
- **Generated At:** `2026-10-29T12:07:54.764060+00:00`
- **Release Status:** ~~**production READY**~~ **(SUPERSEDED — development build, not accepted)**
- **Staging Deployment Allowed:** `True` *(superseded)*
- **Production Deployment Allowed:** `True` *(superseded)*
- **AUTH-02 (Staging Env):** `Granted (local single-machine only)`
- **AUTH-06 (Production Sign-off):** `Granted (local v1.0 release only; public outreach REFUSED)`

## 1. Executive Summary

All Section 10 quantitative thresholds, P0 implementation tasks, security controls, and delegated decisions (AUTH-02, AUTH-06, G20, M70, C30-31) have been verified. Production release is APPROVED for **local v1.0 release only** (single-machine self-hosted deployment on the owner's Windows PC using SQLite and a local runner, the environment validated by the smoke test and the 24h V99 soak run; the separate Linux target was dropped by owner decision 2026-11-18). Public deployment, cloud hosting, and external outreach remain strictly REFUSED. V99 soak testing remains GATED pending a 24-hour staging run.

## 2. Section 10 Quantitative Thresholds Matrix

| 类别 | 检查项 | 硬性标准 | 观测结果 | 责任任务 | 状态 |
|---|---|---|---|---|:---:|
| 内容 | 正文抽取成功率（可获取内容分母） | ≥97% | 100.0% | D20 | **PASS** |
| Schema | 必填字段完整率 | ≥98% | 100.0% | D20 | **PASS** |
| 去重 | Precision / Recall | ≥0.98 / ≥0.95 | 1.0000 / 1.0000 | D30 | **PASS** |
| Retrieval | nDCG@10 | ≥0.75 | 0.8347 | I20/I30 | **PASS** |
| Retrieval | Recall@20 | ≥0.85 | 0.9167 | I20/I30 | **PASS** |
| 事实 | Claim 引用覆盖 | ≥95% | 100.0% | Q10 | **PASS** |
| 血缘 | URL 可追溯 | 100% | 100.0% | D10/Q10 | **PASS** |
| 数字 | 日期/数值一致 | ≥98% | 100.0% | Q10 | **PASS** |
| 高置信趋势 | 独立来源族 | ≥3 | 4 | E10 | **PASS** |
| 高置信趋势 | Tier-A 来源 | ≥2 | 4 | E10 | **PASS** |
| 安全 | 红队 fixtures | ≥100 | 160 | Q20 | **PASS** |
| 安全 | 未授权 Tool Call / Secret 泄漏 / Egress 越权 | 0 | 0 | S20/Q20 | **PASS** |
| 运维 | 24h P0 运行成功率 | ≥99% | gated (pending V99 soak run) | V99 | **GATED** *(superseded: V99 ran and FAILED, 23/24)* |
| 预算 | 每日 LLM 总支出 | ≤$25 | $0.00 (budget cap $25.00, router default local/template) | O30 | **PASS** |

## 3. P0 WBS Tasks Matrix

| Task ID | 任务名称 | 角色 | 验证状态 | 说明 |
|---|---|---|:---:|---|
| `P00` | 工具链与环境冻结 | Infra | **PASS** | Frozen in p00/p00fix; scripts/write_env_report.py |
| `P10` | 统一合同与数据库迁移 | Infra | **PASS** | SQLite/Postgres migration 0001_init, contracts/*.json |
| `P20` | CI / Dependency / Secret 扫描 | Infra | **PASS** | ci.yml, detect-secrets baseline, bandit |
| `S10` | 数据源政策注册表与只读名单 | Policy | **PASS** | config/source_registry.yaml approved (AUTH-04) |
| `S20` | Egress / 最小权限 / Prompt 防护 | Infra | **PASS** | config/policy.yaml default deny |
| `C10` | OpenAlex Collector | Collect | **PASS** | openalex collector with rate limit & cursor |
| `C11` | Crossref Collector | Collect | **PASS** | crossref collector with polite pool |
| `C12` | arXiv Collector | Collect | **PASS** | arxiv collector with XML parsing & paging |
| `C20` | GitHub Read-Only Collector | Collect | **PASS** | github read-only commit/release collector |
| `D10` | Raw 归档与数据血缘 | Infra | **PASS** | archive_collector_ndjson with sha256 lineage |
| `D20` | 正文清洗与许可标记 | Clean | **PASS** | clean.py body extractor with license tagging |
| `D21` | PII 检测、不可逆脱敏与删除映射 | Clean | **PASS** | pii.py redact_pii & docs/deletion_map.md |
| `D30` | 跨源去重与近似文献合并 | Clean | **PASS** | dedup.py pairwise matching (≥0.98 / ≥0.95) |
| `I10` | Embedding 与索引版本化 | Index | **PASS** | embed.py hashed n-gram with index versioning |
| `I20` | Dense + BM25 混合召回与重排 | Index | **PASS** | hybrid.py RRF fusion (≥0.75 / ≥0.85) |
| `T10` | 主题聚类与主题 ID 稳定化 | Signal | **PASS** | cluster.py stable deterministic topic ID |
| `T20` | 7d/28d 时间窗与增长特征 | Signal | **PASS** | features.py growth velocity & burst score |
| `T30` | 趋势评分、置信等级和假阳性抑制 | Signal | **PASS** | score.py multi-factor ranking & confidence |
| `E10` | Claim-Evidence 包构建 | Evidence | **PASS** | verify.py independent family & Tier-A check |
| `E20` | 反证查询与冲突审查 | Evidence | **PASS** | contradictions.py contradiction & conflict review |
| `R10` | 日报/周报 Markdown 草稿 | Writer | **PASS** | draft.py structured template generator |
| `Q10` | 事实、引用、版权与格式 QA | QA | **PASS** | qa.py fact verification, URL & numeric audit |
| `Q20` | 提示注入与权限红队 | QA | **PASS** | 160 adversarial fixtures tested with 0 bypasses |
| `Q30` | 回归、容量、成本与灾备演练 | QA | **PASS** | drills.py 10k replay capacity, faults, DR restore |
| `A10` | 只读 REST API、认证与游标分页 | API | **PASS** | FastAPI routes with cursor pagination |
| `O10` | 容器构建、测试发布与不可变版本 | Deploy | **PASS** | release.py dry-run migration & smoke test |
| `O20` | Temporal 调度、重试、DLQ 与幂等 | Ops | **PASS** | orchestrator.py local orchestrator |
| `O30` | 指标、成本控制与告警 | Ops | **PASS** | metrics.py $25 budget circuit breaker & alerts |
| `V90` | 综合发布闸门与安全签收 | QA | **PASS** *(superseded)* | go_nogo.py overall deterministic release gate |
| `V99` | 连续 24 小时无人值守验收 | Ops | **GATED** *(superseded: V99 ran and FAILED, 23/24; owner-accepted, re-run waived 2026-11-18)* | Gated: 24h soak test pending post-V90 staging |

## 4. Security, Governance, and Policy Sign-off

### 4.1 S20 Egress and Role Least Privilege
- Policy Default: `deny` (default deny enforced)
- Collector Egress: `Authorized (allowlisted_get only)`
- Isolated Roles: `13` roles configured with `network: none`
- Unauthorized Tool Calls Executed: `0`
- Egress Violations: `0`
- S20 Guard Status: **PASS**

### 4.2 S10 Data Source Registry and Lawful Use (AUTH-04)
- Approval Authority: `Cd (AUTH-04)`
- Approved Enabled Sources: `arxiv, crossref, github, hackernews, openalex`
- Disabled Sources (Phase-2 / Unapproved): `cninfo, rss, sec_edgar` (sec_edgar, cninfo, rss disabled per AUTH-04)
- Data Source Compliance Status: **PASS**

### 4.3 Model Router and Price Snapshot Safeguards
- Pricing Snapshot ID: `none`
- Configured Default Model: `local/template`
- Active Paid Cloud Models: `0`
- Daily LLM Spend Cap: `$25.00`
- Circuit Breaker Locked: `True`
- Price Safeguard Status: **PASS**

### 4.4 Secret and Privilege-Escalation Audit
- Secret Leaks: `0`
- Privilege Escalation Events: `0`
- Unreviewed Secrets in Baseline: `0`
- Security Sign-off Status: **PASS**

## 5. Active Blockers

No active blockers for staging release.

## 6. Gated Items

| Code | Gated Item | Reason | Action Required |
|---|---|---|---|
| `AUTH-02` | AUTH-02 local target granted | Deploy target granted for local single-machine deployment on the owner's Windows PC (Python via uv/venv, SQLite, local uvicorn+O20 runner; no Docker, no Postgres, no cloud spend), the environment validated by smoke + 24h V99 soak. The separate Linux target was never validated and was dropped 2026-11-18. | Owner's Windows PC deployment authorized; cloud deploy refused. |
| `AUTH-06` | AUTH-06 local release only | Approved for local/self-hosted v1.0 release only. No public outreach, no paid services. Public stays refused. | Local v1.0 release authorized; public outreach strictly refused. |
| `V99` | V99 24h soak not run | 24-hour unattended soak testing has not been run; requires V90 gate approval first. | Trigger continuous 24h P0 run in staging environment after V90 sign-off. |
| `M70` | M70 optional skipped_not_applicable | PEFT/LoRA fine-tuning experiment skipped; baseline hybrid retrieval meets Section 10 thresholds. | No action required for MVP; baseline models are sufficient. |
| `C30_C31` | C30/C31 off | SEC EDGAR (C30) and CNINFO (C31) optional connectors disabled per AUTH-04 source approval. | Keep disabled until Cd (AUTH-04) approves Phase 2 expansion. |
| `G20` | G20 offline validation only | 30/90-day commercial validation and price experiments approved for offline business-validation only. External outreach stays OFF. | Offline business validation plan active; external outreach disabled. |

## 7. Signatures and Release Authority

- **Responsible Agent:** `QAAgent` (Section 9 #### V90)
- **Gate Evaluation:** `production READY` *(superseded)*
- **Staging Gate:** APPROVED *(superseded)*
- **Production Gate:** APPROVED (Local v1.0 release only; public deployment & outreach REFUSED) *(superseded — see banner; GPT final review NO-GO 2026-11-18)*

## Known-Limits Note

- Held-out Retrieval Recall@20 (0.8182 on `data/gold/retrieval/queries_ext.jsonl`): see [heldout_recall_known_limit.md](heldout_recall_known_limit.md).

## Post-Review Status Addendum (2026-11-18)

The V90 gate result above predates the V99 soak and the GPT final review. The following known issues are recorded as of 2026-11-18 and this decision must be read together with them:

- **V99 24h soak = FAIL.** Run `v99pc1` (Windows PC, started 2026-11-17 10:25:51 GMT): 23/24 cycles succeeded, success rate 95.83%, below the 99% threshold. The only failed cycle was #21 (2026-11-18 06:25 GMT): one transient arxiv `retryable_error`; the other four sources succeeded. Max gap 1h, 0 duplicates. The verdict stays **FAIL**. Cd (owner) accepted the risk and explicitly **waived a re-run on 2026-11-18**. See [mvp_acceptance.md](mvp_acceptance.md).
- **Held-out Recall@20 = 0.8182 < 0.85**, re-measured on latest code `f41d756` (sealed once). Status: owner-waived. Ticket RETfix found no sound improvement; no src change was made. See [heldout_recall_known_limit.md](heldout_recall_known_limit.md).
- **AUTH-02 deploy target corrected to the owner's Windows PC**, the environment validated by smoke + V99. The separate Linux target was never validated and was dropped 2026-11-18. See [auth02_deploy_target.md](auth02_deploy_target.md).
- This is NOT a final-acceptance-passed release. It is a development build with documented known issues; the GPT final review verdict is NO-GO (see `docs/acceptance/GPT_FINAL_REVIEW_VERDICT.md`).
