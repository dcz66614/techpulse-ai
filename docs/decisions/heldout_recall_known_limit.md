# Decision: Held-out Retrieval Recall@20 Known Limit (Ticket DECkl)

- **Date:** 2026-10-29
- **Decided-by:** project owner via the dispatcher
- **Ticket:** DECkl
- **Status:** Accepted Known Limit for Local v1.0 Release

## Context

An extended gold eval set (`data/gold/retrieval/queries_ext.jsonl`, merged on master as GQ/GQX). Labels by `glm-5.3-flash-free`, cross-checked by `big-pickle`, split into `dev2`/`heldout`. Thresholds: nDCG@10 >= 0.75, Recall@20 >= 0.85.

## Scores (exact)

- dev, 8 queries: nDCG@10 0.8347, Recall@20 0.9167 (pass)
- dev2, 13 queries: 0.8572 / 1.0000 (pass)
- heldout, 11 queries: 0.8954 / 0.8182 (Recall@20 FAIL)
- per language, heldout: en 0.9566/0.8750, zh 0.8605/0.7857

| Split | Queries | nDCG@10 | Recall@20 | Status |
|---|---|---|---|:---:|
| `dev` | 8 | 0.8347 | 0.9167 | **PASS** |
| `dev2` | 13 | 0.8572 | 1.0000 | **PASS** |
| `heldout` | 11 | 0.8954 | 0.8182 | **Recall@20 FAIL** |

Per-language breakdown on the held-out split (11 queries):
- `en`: nDCG@10 0.9566 / Recall@20 0.8750
- `zh`: nDCG@10 0.8605 / Recall@20 0.7857

## Decision

- Do NOT tune retrieval in response to held-out results (that would leak held-out into development).
- Accept Recall@20 0.8182 on held-out as a KNOWN LIMIT of the local v1.0 release.
- Thresholds stay unchanged.
- The sealed TEST split stays untouched.

## Consequences

- zh recall is weaker; future work would add more dev queries and improve retrieval, tuned on dev/dev2 only, with a fresh held-out set.

## Re-measured on f41d756 (2026-11-18)

The held-out split was re-measured once, sealed, on the latest code commit `f41d756`. The scores are unchanged: held-out nDCG@10 0.8954 (>= 0.75, ok) and Recall@20 0.8182 (< 0.85 threshold). Ticket RETfix (builder hy3) tried 5 principled variants tuned on dev/dev2 only and never touching held-out; it found no sound improvement and made no src change. Recall@20 therefore stays below threshold. Status: **owner-waived**. Thresholds were not relaxed.

| Split | Queries | nDCG@10 | Recall@20 | Status |
|---|---|---|---|:---:|
| `dev` | 8 | 0.8347 | 0.9167 | PASS |
| `dev2` | 13 | 0.8572 | 1.0000 | PASS |
| `heldout` | 11 | 0.8954 | 0.8182 | Recall@20 FAIL — owner-waived |
| `heldout en` | — | 0.9566 | 0.8750 | — |
| `heldout zh` | — | 0.8605 | 0.7857 | — |

- **Decided-by:** Owner (Cd), 2026-11-18
- Evidence: `docs/acceptance/evidence/split_eval_f41d756.txt`
