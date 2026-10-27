# Source Policy Decision: arXiv (`arxiv`)

- **Source ID:** `arxiv`
- **Source Family:** `academic_papers`
- **Status:** Approved (Pending final Cd / AUTH-04 sign-off)
- **Enabled:** `true`
- **Verification Timestamp (checked_at):** `2026-10-27T22:59:00Z`
- **Policy Snapshot ID:** `sha256-74aca38a215f3c08`
- **Approval Authority:** Cd (`AUTH-04`)

## 1. Official Documentation & Evidence URLs

1. **API Overview:** [https://info.arxiv.org/help/api/index.html](https://info.arxiv.org/help/api/index.html)
2. **Terms of Use (ToU):** [https://info.arxiv.org/help/api/tou.html](https://info.arxiv.org/help/api/tou.html)
3. **API User Manual:** [https://info.arxiv.org/help/api/user-manual.html](https://info.arxiv.org/help/api/user-manual.html)
4. **Robots Exclusion (Main Site):** [https://arxiv.org/robots.txt](https://arxiv.org/robots.txt)
5. **Robots Exclusion (Export Gateway):** [https://export.arxiv.org/robots.txt](https://export.arxiv.org/robots.txt)

## 2. Allowed Endpoints & HTTP Methods

- **Allowed HTTP Method:** `GET` only.
- **Allowed Base URL:** `https://export.arxiv.org/api/query` (or `http://export.arxiv.org/api/query`)
- **Allowed Endpoints:**
  - `https://export.arxiv.org/api/query`

## 3. Domain Whitelist

Strict egress allow-list:
- `export.arxiv.org`

Calls to `arxiv.org` web frontend endpoints or indiscriminately scraping HTML pages are forbidden. Note that `export.arxiv.org/robots.txt` disallows generic web crawlers (`Disallow: /`), whereas the API query interface (`/api/query`) is explicitly authorized by arXiv's Terms of Use for API clients.

## 4. Rate Limits & Polite Pool

- **Strict Request Interval:** No more than one request every three seconds (maximum 20 requests per minute).
- **Concurrency Limit:** 1 single connection at a time across all machines under system control.
- **Whole-Organization Scope:** Rate limits apply across all machines as a whole; horizontal scaling of IP addresses or distributed workers to bypass rate limits is strictly forbidden.
- **Paging Limits:**
  - Maximum results per query: 30,000 results.
  - Maximum results per slice: 2,000 items (using `start` and `max_results`).
- **Pacing Rule:** Every collector worker accessing arXiv must enforce a mandatory sleep of at least 3.0 seconds between consecutive requests.

## 5. Required Headers & Authentication

- **User-Agent:** Descriptive User-Agent identifying the client and contact email:
  `TechPulse-AI/1.0 (https://github.com/techpulse; contact: info@techpulse.example.com)`
- **Authentication:** None required. Public query endpoint.

## 6. Attribution, Caching & Redistribution Rights

- **Descriptive Metadata License:** CC0 1.0 Universal Public Domain Dedication.
  - Descriptive metadata includes discovery and identification information: title, abstract, authors, identifiers, and classification terms.
  - Retention, caching, and inclusion in analytical products is fully permitted under CC0.
- **Full Text / PDF Redistribution Prohibition:**
  - Full-text e-print PDFs and TeX source files are subject to copyright protection.
  - **Redistribution of e-print PDFs or source files from TechPulse servers is strictly prohibited** unless individual explicit licenses permit.

## 7. Prohibited Behaviors & Error Handling

- **Prohibitions:**
  - Making more than 1 request per 3 seconds.
  - Making multiple concurrent connections.
  - Increasing the number of machines to overcome rate limits.
  - Storing and publicly serving arXiv e-print PDFs or source files.
  - Indiscriminate automated scraping of HTML pages on `arxiv.org`.
- **Error Handling & Rollback:**
  - On HTTP 503 / 429: Immediately halt requests, back off for a minimum of 60 seconds, and retry at reduced pace.
  - On persistent error: Transition collector to `policy_blocked` and alert operator.

## 8. Verbatim Supporting Quotes

> "You are free to use descriptive metadata1 about arXiv e-prints under the terms of the Creative Commons Universal (CC0 1.0) Public Domain Declaration."  
> — *arXiv API Terms of Use (info.arxiv.org/help/api/tou.html)*

> "1Descriptive metadata includes information for discovery and identification purposes, and includes fields such as title, abstract, authors, identifiers, and classification terms."  
> — *arXiv API Terms of Use (info.arxiv.org/help/api/tou.html)*

> "When using the legacy APIs (including OAI-PMH, RSS, and the arXiv API), make no more than one request every three seconds, and limit requests to a single connection at a time."  
> — *arXiv API Terms of Use (info.arxiv.org/help/api/tou.html)*

> "Please note that the following rate limits apply to all of the machines under your control as a whole. You should not attempt to overcome these limits by increasing the number of machines used to make requests."  
> — *arXiv API Terms of Use (info.arxiv.org/help/api/tou.html)*

> "You understand that e-prints from arXiv are subject to all applicable copyright protections. The redistribution of e-prints requires permission from the copyright holder, which may be the author(s) or publisher. arXiv is not the copyright holder on any of the e-prints available through the API... Store and serve arXiv e-prints (PDFs, source files, or other content) from your servers, unless you have the permission of the copyright holder or are permitted to do so by the license with which the e-print was submitted."  
> — *arXiv API Terms of Use (info.arxiv.org/help/api/tou.html)*

> "The request can be made via HTTP GET, in which the parameters are encoded in the url"  
> — *arXiv API User Manual (info.arxiv.org/help/api/user-manual.html)*

> "Because of speed limitations in our implementation of the API, the maximum number of results returned from a single call (max_results) is limited to 30000 in slices of at most 2000 at a time, using the max_results and start query parameters."  
> — *arXiv API User Manual (info.arxiv.org/help/api/user-manual.html)*

> "In cases where the API needs to be called multiple times in a row, we encourage you to play nice and incorporate a 3 second delay in your code."  
> — *arXiv API User Manual (info.arxiv.org/help/api/user-manual.html)*
