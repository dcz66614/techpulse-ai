# Acceptance archive

This directory carries the final-review documentation and the small text evidence that
backs it up. It is tracked in git, unlike `.orchestra/` and `artifacts/` (git-ignored),
 so the evidence survives a push.

- `GPT_FINAL_REVIEW_PACKAGE.md` — the package assembled for GPT final review. Its V99
  section is superseded by the final result below.
- `GPT_FINAL_REVIEW_VERDICT.md` — the NO-GO verdict (2026-11-18), its five reasons and
  each resolution (`fixed` / `owner-waived` / `DUP_STATUS_PLACEHOLDER`).
- `evidence/` — small text evidence only (json/jsonl/md/txt/log, each < 200KB):
  - `evidence/v99pc1/` — soak run `v99pc1` (Windows PC): `soak_test.json`, `cycles.jsonl`,
    `manifest.json`, `metrics.json`, `heartbeat.json`, `soak_state.json`, `soak.log`,
    `mvp_acceptance_generated.md` and status output. The raw generated file
    `mvp_acceptance_generated.md` is unmodified except for a top-level SUPERSEDED / NOTE
    banner: its AUTH-02 deploy target was corrected to the owner's Windows PC on
    2026-11-18 (see `docs/decisions/auth02_deploy_target.md`); the original body text is
    retained verbatim for provenance.
  - `evidence/split_eval_f41d756.txt` — held-out split re-measurement on `f41d756`.

v99box3 / v99box4 evidence will be added later by the dispatcher. Binary databases
(`*.sqlite`, `*.db`) are intentionally not archived here.
