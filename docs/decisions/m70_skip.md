# Decision: M70 Model Adaptation Experiment (Skipped Not Applicable)

- **Decision:** Task M70 (PEFT/LoRA fine-tuning experiment) is marked `skipped_not_applicable` (optional task).
- **Rationale:** Baseline hybrid retrieval (Dense hashed n-gram + BM25 with RRF fusion) achieves nDCG@10 of 0.8347 (threshold >= 0.75) and Recall@20 of 0.9167 (threshold >= 0.85) on real evidence datasets, comfortably exceeding Section 10 criteria. Fine-tuning an LLM via LoRA introduces operational complexity, GPU compute requirements, and maintenance overhead without demonstrable necessity for the v1.0 release.
- **Scope limits:** Applies specifically to optional task M70 PEFT/LoRA fine-tuning. Base embedder and prompt template rendering continue to operate in current deterministic configuration.
- **Date:** 2026-10-29
- **Decided-by:** dispatcher, delegated by Cd
