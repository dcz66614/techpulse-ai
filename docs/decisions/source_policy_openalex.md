# Source Policy Decision: OpenAlex (`openalex`)

- **Source ID:** `openalex`
- **Source Family:** `academic_papers`
- **Status:** Approved (Pending final Cd / AUTH-04 sign-off)
- **Enabled:** `true`
- **Verification Timestamp (checked_at):** `2026-10-27T22:55:00Z`
- **Policy Snapshot ID:** `sha256-479109e8c4efbcec`
- **Approval Authority:** Cd (`AUTH-04`)

## 1. Official Documentation & Evidence URLs

1. **Authentication & Rate Limits:** [https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication) (Canonical markdown: [https://help.openalex.org/api/authentication.md](https://help.openalex.org/api/authentication.md))
2. **Pricing & Sustainability:** [https://help.openalex.org/access/pricing.md](https://help.openalex.org/access/pricing.md)
3. **Data Overview & License:** [https://docs.openalex.org/about-the-data/data-overview](https://docs.openalex.org/about-the-data/data-overview)
4. **Robots Exclusion:** [https://api.openalex.org/robots.txt](https://api.openalex.org/robots.txt)
5. **LLMs Entry:** [https://help.openalex.org/llms.txt](https://help.openalex.org/llms.txt)

## 2. Allowed Endpoints & HTTP Methods

- **Allowed HTTP Method:** `GET` only.
- **Allowed Base URL:** `https://api.openalex.org`
- **Allowed Endpoints:**
  - `https://api.openalex.org/works`
  - `https://api.openalex.org/works/{id}`
  - `https://api.openalex.org/authors`
  - `https://api.openalex.org/authors/{id}`
  - `https://api.openalex.org/sources`
  - `https://api.openalex.org/sources/{id}`
  - `https://api.openalex.org/institutions`
  - `https://api.openalex.org/institutions/{id}`
  - `https://api.openalex.org/publishers`
  - `https://api.openalex.org/publishers/{id}`
  - `https://api.openalex.org/funders`
  - `https://api.openalex.org/funders/{id}`
  - `https://api.openalex.org/topics`
  - `https://api.openalex.org/topics/{id}`
  - `https://api.openalex.org/rate-limit`

## 3. Domain Whitelist

Strict egress allow-list:
- `api.openalex.org`

Any requests to domains outside this whitelist must be rejected by the network/egress proxy.

## 4. Rate Limits & Polite Pool

- **Keyless (Casual) Tier:**
  - Rate limit: 10 requests per second (600 req/min derived from the quoted per-second rate).
  - Basic paging limit: Up to 10,000 results.
- **Free API Key Tier:**
  - Rate limit: 30 requests per second (1,800 req/min derived from the quoted per-second rate).
  - Daily budget: $1.00 USD/day free allocation ("with a free account's $1 a day").
  - Paging: Cursor paging enabled for results beyond 10,000.
- **Query Paging Limits:**
  - `per_page` maximum: 100.
  - `sample` maximum: 10,000.
- **Concurrency Limit:** `not stated` (the official documentation gives no numeric concurrency limit).
- **Response Headers Monitored:**
  - `X-RateLimit-Limit`: Daily quota limit.
  - `X-RateLimit-Remaining`: Remaining budget.
  - `X-RateLimit-Credits-Used`: Credits used by current call.
  - `X-RateLimit-Reset`: Seconds until reset at midnight UTC.
- **Collector Throttling Target (project choice, not an official limit):** Conservative default of 60–120 requests/minute to respect shared server capacity.

## 5. Required Headers & Authentication

- **User-Agent:** Polite User-Agent identifying the system and technical contact:
  `TechPulse-AI/1.0 (https://github.com/techpulse; contact: info@techpulse.example.com)`
- **Authentication:**
  - Optional for casual scraping/testing within 10 req/s.
  - Free API key can be supplied as `Authorization: Bearer <API_KEY>` or query parameter `api_key=<API_KEY>`.

## 6. Attribution, Caching & Redistribution Rights

- **License:** CC0 1.0 Universal Public Domain Dedication.
- **Redistribution Rights:** Full metadata reuse and redistribution permitted without permission or license restrictions.
- **Caching & Retention:** Persistent caching in Raw Object Storage and PostgreSQL is fully authorized under CC0.
- **Attribution:** Attribution to OpenAlex and following POSI principles is recommended best practice.

## 7. Prohibited Behaviors & Error Handling

- **Prohibitions:**
  - Exceeding daily budget or rate limits (returns HTTP `429 Too Many Requests`).
  - Inventing fake email addresses to claim automated API keys.
  - Automated crawling of HTML search and list pages on `openalex.org` (robots disallow scripts on list pages; official REST API must be used).
- **Error Handling & Rollback:**
  - On HTTP 429: Apply exponential backoff according to `Retry-After` / `X-RateLimit-Reset`.
  - On HTTP 401/403 or policy block: Transition collector state to `policy_blocked`, freeze cursor, and notify operations.

## 8. Verbatim Supporting Quotes

> "OpenAlex data is free, and so is casual use of the API — you can make basic queries with no key at all. To use the API at any real scale, though, you'll want a free API key: it raises your daily budget 10×, lets you track your usage, and is required for cursor paging past the first page."  
> — *OpenAlex Documentation: Authentication (help.openalex.org/api/authentication.md)*

> "The API is plain GET requests, so you can test any query in your browser"  
> — *OpenAlex Documentation: Best practices (help.openalex.org/api/authentication.md)*

> "Who’s calling: No API key -> Requests per second: 10; Free API key, or prepaid usage -> Requests per second: 30; Annual plan -> Requests per second: 50"  
> — *OpenAlex Documentation: Rate limits (help.openalex.org/api/authentication.md)*

> "Two things return 429 Too Many Requests: exceeding your daily budget, or sending requests faster than your per-second limit."  
> — *OpenAlex Documentation: Rate limits (help.openalex.org/api/authentication.md)*

> "Every response carries headers and a meta block showing where you stand: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Credits-Used, X-RateLimit-Reset"  
> — *OpenAlex Documentation: Keeping tabs on your usage (help.openalex.org/api/authentication.md)*

> "Scholarly metadata is treated in most markets as 'the new oil' — value is captured by limiting access. OpenAlex takes the opposite position: the data is a public good, released under a CC0 public-domain license with no 'personal use only' carve-out and no permission to ask. Revenue comes from time and services layered on top — serving, freshness, support — rather than from the data itself, mirroring the POSI principle of revenue based on services, not data."  
> — *OpenAlex Documentation: Free data, paid services (help.openalex.org/access/pricing.md)*

> "Ask your user for their email address; never invent one. We email that address, and the key works only after its owner clicks the link."  
> — *OpenAlex Documentation: Getting a key for your user (help.openalex.org/api/authentication.md)*

> "Record pages on openalex.org are open to agents and scripts... Search and list pages on openalex.org are not served to scripts. The API has more data and is faster"  
> — *OpenAlex Documentation: Access (help.openalex.org/llms.txt)*
