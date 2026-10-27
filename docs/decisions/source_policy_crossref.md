# Source Policy Decision: Crossref (`crossref`)

- **Source ID:** `crossref`
- **Source Family:** `academic_papers`
- **Status:** Approved (Pending final Cd / AUTH-04 sign-off)
- **Enabled:** `true`
- **Verification Timestamp (checked_at):** `2026-10-27T22:56:00Z`
- **Policy Snapshot ID:** `sha256-d32d7500c7317bfb`
- **Approval Authority:** Cd (`AUTH-04`)

## 1. Official Documentation & Evidence URLs

1. **REST API Documentation:** [https://www.crossref.org/documentation/retrieve-metadata/rest-api/](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
2. **Tips for Using the REST API:** [https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/](https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/)
3. **OpenAPI / Swagger Specification:** [https://api.crossref.org/swagger-docs](https://api.crossref.org/swagger-docs)
4. **Robots Exclusion:** [https://www.crossref.org/robots.txt](https://www.crossref.org/robots.txt)
5. **Metadata Plus Overview:** [https://www.crossref.org/documentation/metadata-plus/](https://www.crossref.org/documentation/metadata-plus/)

## 2. Allowed Endpoints & HTTP Methods

- **Allowed HTTP Method:** `GET` only (HTTP HEAD also permitted for singleton presence checks).
- **Allowed Base URL:** `https://api.crossref.org`
- **Allowed Endpoints:**
  - `https://api.crossref.org/works`
  - `https://api.crossref.org/works/{doi}`
  - `https://api.crossref.org/works/{doi}/agency`
  - `https://api.crossref.org/journals`
  - `https://api.crossref.org/journals/{issn}`
  - `https://api.crossref.org/journals/{issn}/works`
  - `https://api.crossref.org/members`
  - `https://api.crossref.org/members/{id}`
  - `https://api.crossref.org/members/{id}/works`
  - `https://api.crossref.org/prefixes/{prefix}/works`
  - `https://api.crossref.org/funders`
  - `https://api.crossref.org/funders/{id}`
  - `https://api.crossref.org/funders/{id}/works`
  - `https://api.crossref.org/types`
  - `https://api.crossref.org/types/{id}/works`
  - `https://api.crossref.org/licenses`

## 3. Domain Whitelist

Strict egress allow-list:
- `api.crossref.org`

Any requests to other hosts or IP addresses are strictly prohibited.

## 4. Rate Limits & Polite Pool

- **Polite Pool Rules:**
  - Supplying a valid `mailto` parameter or contact in the `User-Agent` routes the request to the `polite-array`.
  - Rate limit: 3 requests per second (`x-rate-limit-limit: 3`, `x-rate-limit-interval: 1s`).
  - Concurrency limit: 3 concurrent requests (`x-concurrency-limit: 3`).
  - Requests per minute ceiling: 180 requests/minute.
- **Public Pool (Fallback if mailto omitted):**
  - Limit: 1 request per second (`x-rate-limit-limit: 1`).
  - Concurrency limit: 1 (`x-concurrency-limit: 1`).
- **Paging Limits & Cursors:**
  - `rows` per request: Maximum 1,000 items.
  - Deep pagination: Must use cursor pagination (`cursor=*` followed by `cursor=[next-cursor]`).
  - Cursor validity: Cursors expire after 5 minutes if not accessed.
- **Collector Target:** Set to 60–120 requests/minute in the polite pool.

## 5. Required Headers & Authentication

- **User-Agent:** Must include contact email and application name:
  `TechPulse-AI/1.0 (mailto:info@techpulse.example.com)`
- **Query Parameter:** Append `mailto=info@techpulse.example.com` to all requests to guarantee routing to the polite pool.
- **Accept Header:** `application/vnd.crossref-api-message+json`
- **Authentication:** None required for public REST API polite pool. (Metadata Plus is an optional commercial paid subscription).

## 6. Attribution, Caching & Redistribution Rights

- **License & Community Sharing:** Crossref members deposit metadata which is openly shared with the global scholarly community through free APIs. Site documentation is licensed under CC BY 4.0.
- **Caching & Retention:** Caching is explicitly encouraged: "You might have a request that you want to make repeatedly and keep local results cached. You may even want to have a complete copy of the Crossref database and keep it up-to-date".
- **Redistribution Rights:** Bibliographic metadata may be stored, indexed, and incorporated into analytical outputs.

## 7. Prohibited Behaviors & Error Handling

- **Prohibitions:**
  - Exceeding rate limits without backoff.
  - Continuing requests after receiving HTTP 429 or 403 blocks.
- **Error Handling & Rollback:**
  - On HTTP 429: Immediately halt requests and wait for rate-limit cooldown.
  - On HTTP 401/403: Stop collector, mark `policy_blocked`, do not cycle IPs or bypass restrictions.

## 8. Verbatim Supporting Quotes

> "Our publicly available REST API exposes the scholarly metadata that members and trusted sources deposit with Crossref. This bibliographic metadata and much more—you will find funding data, license information, post-publication updates, ORCID and ROR IDs, and abstracts. You can search, facet, filter, or sample metadata from dozens of thousands of members from all around the world, and the results are returned in JSON format."  
> — *Crossref Documentation: REST API (crossref.org/documentation/retrieve-metadata/rest-api/)*

> "Use the polite pool: https://api.crossref.org/works?rows=0&mailto=yourmail@company.org"  
> — *Crossref Documentation: REST API Examples (crossref.org/documentation/retrieve-metadata/rest-api/)*

> "'in': 'query', 'name': 'mailto', 'description': \"The email address to identify yourself and access the 'polite pool'\""  
> — *Crossref OpenAPI Specification (api.crossref.org/swagger-docs)*

> "List requests return up to 1000 items in a single request. Paginating through multiple pages of results is possible using the cursor parameter... Cursors expire after 5 minutes if not used."  
> — *Crossref OpenAPI Specification (api.crossref.org/swagger-docs)*

> "Also pay attention to the http status code and back off if you start seeing 429 statuses, which means you are being rate limited."  
> — *Crossref Documentation: Tips for using the Crossref REST API (crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/)*

> "If you see a 429 or 403 response it means you have been temporarily or permanently blocked, in which case check the response text for details on how to proceed."  
> — *Crossref OpenAPI Specification: Response codes (api.crossref.org/swagger-docs)*

> "You might have a request that you want to make repeatedly and keep local results cached. You may even want to have a complete copy of the Crossref database and keep it up-to-date"  
> — *Crossref Documentation: Tips for using the Crossref REST API (crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/)*

> "All this metadata is openly shared with the global scholarly community through our free APIs."  
> — *Crossref Documentation: Metadata Plus (crossref.org/documentation/metadata-plus/)*
