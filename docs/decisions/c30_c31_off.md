# Decision: C30 and C31 Optional Connectors (Stay OFF)

- **Decision:** SEC EDGAR (C30) and CNINFO (C31) optional data connectors remain disabled (stay OFF) per AUTH-04 source approval.
- **Rationale:** AUTH-04 source approval (2026-10-28) authorized five core sources (OpenAlex, Crossref, arXiv, GitHub, HackerNews) and explicitly disabled sec_edgar, cninfo, and rss. SEC EDGAR and CNINFO handle specialized regulatory and financial filings that are outside the primary scientific and technical scope of the MVP. Keeping them disabled maintains minimal attack surface, eliminates unnecessary crawler traffic, and upholds strict source compliance.
- **Scope limits:** SEC EDGAR (C30) and CNINFO (C31) remain disabled in `config/source_registry.yaml`. No live ingestion or scheduled syncs from these sources.
- **Date:** 2026-10-29
- **Decided-by:** dispatcher, delegated by Cd
