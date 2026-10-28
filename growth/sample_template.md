# TechPulse-AI 合规样报模板（骨架）

> **标注：AI 生成。**
> 本文件是 G10-prep 模板，不是真实样报。所有尖括号占位符必须由 R10 与 Q10 PASS 后的真实证据替换；在替换前不得对外展示。
> 模板中不包含任何真实事实、数字、客户案例或可点击链接。

## 报告元数据

- report_id: <REPORT_ID>
- title: <REPORT_TITLE>
- topic: <TOPIC_NAME>
- report_type: <DAILY_OR_WEEKLY>
- as_of: <AS_OF_TIMESTAMP>
- generated_at: <GENERATED_AT_TIMESTAMP>
- language: <LANGUAGE_CODE>
- model_version: <MODEL_VERSION>
- prompt_version: <PROMPT_VERSION>
- evidence_snapshot_id: <EVIDENCE_SNAPSHOT_ID>
- q10_status: PENDING
- qa_pass: false
- publication_status: DRAFT
- ai_label: AI 生成

## 1. 摘要

<SUMMARY_TEXT>

## 2. 趋势评分与置信度

- topic: <TOPIC_NAME>
- trend_score: <TREND_SCORE>
- experimental_score: <TRUE_FALSE>
- confidence: <HIGH_MEDIUM_LOW>
- confidence_rationale: <CONFIDENCE_RATIONALE>
- sample_sufficiency: <SUFFICIENT_OR_INSUFFICIENT>
- numerator: <NUMERATOR>
- denominator: <DENOMINATOR>
- time_window: <TIME_WINDOW>
- score_breakdown:
  - growth: <GROWTH_COMPONENT>
  - source_diversity: <SOURCE_DIVERSITY_COMPONENT>
  - source_quality: <SOURCE_QUALITY_COMPONENT>
  - persistence: <PERSISTENCE_COMPONENT>
  - developer_activity: <DEVELOPER_ACTIVITY_COMPONENT>
  - anomaly_penalty: <ANOMALY_PENALTY>

## 3. 主要 Claim 与证据

### <CLAIM_ID_1>

- claim_id: <CLAIM_ID_1>
- claim_text: <CLAIM_TEXT_1>
- claim_type: <FACT_OR_HYPOTHESIS>
- evidence_ids: [<EVIDENCE_ID_1>, <EVIDENCE_ID_2>]
- source_urls:
  - https://<SOURCE_HOST_1>/<SOURCE_PATH_1>
  - https://<SOURCE_HOST_2>/<SOURCE_PATH_2>
- source_families: [<SOURCE_FAMILY_1>, <SOURCE_FAMILY_2>]
- source_published_at: <SOURCE_PUBLISHED_AT_1>
- retrieved_at: <RETRIEVED_AT_1>
- license_status: <LICENSE_STATUS_1>
- supports: <YES_NO>
- notes: <CLAIM_NOTES_1>

### <CLAIM_ID_2>

- claim_id: <CLAIM_ID_2>
- claim_text: <CLAIM_TEXT_2>
- claim_type: <FACT_OR_HYPOTHESIS>
- evidence_ids: [<EVIDENCE_ID_3>]
- source_urls:
  - https://<SOURCE_HOST_3>/<SOURCE_PATH_3>
- source_families: [<SOURCE_FAMILY_3>]
- source_published_at: <SOURCE_PUBLISHED_AT_2>
- retrieved_at: <RETRIEVED_AT_2>
- license_status: <LICENSE_STATUS_2>
- supports: <YES_NO>
- notes: <CLAIM_NOTES_2>

## 4. 反证

### <COUNTER_CLAIM_ID_1>

- counter_claim_id: <COUNTER_CLAIM_ID_1>
- counter_claim_text: <COUNTER_CLAIM_TEXT_1>
- evidence_ids: [<COUNTER_EVIDENCE_ID_1>]
- source_urls:
  - https://<COUNTER_SOURCE_HOST_1>/<COUNTER_SOURCE_PATH_1>
- source_families: [<COUNTER_SOURCE_FAMILY_1>]
- retrieved_at: <COUNTER_RETRIEVED_AT_1>
- why_it_matters: <COUNTER_RATIONALE_1>
- impact_on_confidence: <CONFIDENCE_IMPACT_1>

## 5. 未知项与证据缺口

- unknown_items:
  - <UNKNOWN_1>
  - <UNKNOWN_2>
- contrary_search_status: <COMPLETE_OR_INCOMPLETE>
- missing_evidence_notes: <EVIDENCE_GAP_NOTES>

## 6. 方法与可复现性

- retrieval_method: <RETRIEVAL_METHOD>
- dedup_method: <DEDUP_METHOD>
- scoring_formula_version: <SCORING_FORMULA_VERSION>
- source_registry_version: <SOURCE_REGISTRY_VERSION>
- known_limitations: <LIMITATIONS>

## 7. 链接目录

- evidence index: <EVIDENCE_INDEX_REFERENCE>
- raw manifest: <RAW_MANIFEST_REFERENCE>
- source URLs: 见第 3、4 节占位符

## 8. QA 与发布门禁

- q10_status: PENDING
- q10_manifest: <Q10_MANIFEST_REFERENCE>
- qa_pass: false
- publication_blocked: true
- blocking_reasons:
  - <BLOCKING_REASON_1>
- required_before_publish:
  - Q10 PASS
  - 所有 Claim 可定位 evidence_id
  - 所有 source_url 替换为真实可追溯链接
  - AUTH-06 对外发送批准（如用于外部邀请）
