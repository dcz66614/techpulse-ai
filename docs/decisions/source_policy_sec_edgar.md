# Source Policy Decision: SEC EDGAR (`sec_edgar`)

- **Source ID:** `sec_edgar`
- **Source Family:** `regulatory_filings`
- **Status:** Disabled (Phase 2 Source; pending Cd / AUTH-04 activation)
- **Enabled:** `false`
- **Disable Reason:** Phase 2 source; must stay enabled: false per project execution plan; requires Cd (AUTH-04) approval before activation.
- **Verification Timestamp (checked_at):** `2026-10-27T23:00:50Z`
- **Policy Snapshot ID:** `sha256-6af9be447cf4062a`
- **Approval Authority:** Cd (`AUTH-04`)

## 1. Official Documentation & Evidence URLs

1. **Accessing EDGAR Data (Developer Guide):** [https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data) (also redirected from [https://www.sec.gov/os/accessing-edgar-data](https://www.sec.gov/os/accessing-edgar-data))
2. **Robots Exclusion:** [https://www.sec.gov/robots.txt](https://www.sec.gov/robots.txt)
3. **Internet Security Policy & Webmaster FAQ:** Referenced on SEC EDGAR fair access portal.

## 2. Allowed Endpoints & HTTP Methods

- **Allowed HTTP Method:** `GET` only.
- **Allowed Base URLs:**
  - `https://data.sec.gov`
  - `https://www.sec.gov`
- **Allowed Endpoints (When activated in Phase 2):**
  - `https://data.sec.gov/submissions/CIK{cik10}.json`
  - `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json`
  - `https://data.sec.gov/api/xbrl/companyconcept/CIK{cik10}/{taxonomy}/{tag}.json`
  - `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filename}`

## 3. Domain Whitelist

Strict egress allow-list:
- `data.sec.gov`
- `www.sec.gov`

## 4. Rate Limits & Polite Pool

- **Fair Access Limit:** Maximum 10 requests per second.
  - "Current max request rate: 10 requests/second."
  - "To ensure everyone has equitable access to SEC EDGAR content, please use efficient scripting. Download only what you need and please moderate requests to minimize server load."
  - "SEC reserves the right to limit request rates to preserve fair access for all users."
- **Requests per minute ceiling:** 600 requests/minute (derived from the quoted 10 requests/second fair access limit; conservative target 120–300 req/min is a project choice, not an official limit).

## 5. Required Headers & Authentication

- **User-Agent Header (Strictly Enforced):**
  - The SEC enforces declared user-agent format with technical contact email:
    `Sample Company Name AdminContact@<sample company domain>.com`
    Example for TechPulse: `TechPulse-AI Collector info@techpulse.example.com`
  - Requests with missing or undeclared user-agents are rejected or rate-limited.
- **Required Headers:**
  - `User-Agent`: Must include company/project name and email address.
  - `Accept-Encoding`: `gzip, deflate`
  - `Host`: `data.sec.gov` or `www.sec.gov`
- **Authentication:** None. Open access subject to fair access policy and declared headers.

## 6. Attribution, Caching & Redistribution Rights

- **Public Domain Data:** Filings submitted by public companies to SEC EDGAR are public domain government records.
- **Caching & Retention:** Caching is strongly encouraged by SEC to reduce server load ("Download only what you need and please moderate requests to minimize server load").
- **Redistribution Rights:** Public disclosure information may be redistributed and analyzed.

## 7. Prohibited Behaviors & Error Handling

- **Prohibitions:**
  - Exceeding 10 requests per second.
  - "The SEC does not allow botnets or automated tools to crawl the site. Any request that has been identified as part of a botnet or an automated tool outside of the acceptable policy will be managed to ensure fair access for all users."
  - Crawling without declared user-agent headers.
- **Phase 2 Status:** Must remain `enabled: false` during Phase 1. Any activation requires Cd (`AUTH-04`) formal review and written sign-off.

## 8. Verbatim Supporting Quotes

> "Fair access: Current max request rate: 10 requests/second."  
> — *SEC.gov: Accessing EDGAR Data (sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)*

> "To ensure everyone has equitable access to SEC EDGAR content, please use efficient scripting. Download only what you need and please moderate requests to minimize server load."  
> — *SEC.gov: Accessing EDGAR Data (sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)*

> "SEC reserves the right to limit request rates to preserve fair access for all users."  
> — *SEC.gov: Accessing EDGAR Data (sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)*

> "The SEC does not allow botnets or automated tools to crawl the site. Any request that has been identified as part of a botnet or an automated tool outside of the acceptable policy will be managed to ensure fair access for all users."  
> — *SEC.gov: Accessing EDGAR Data (sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)*

> "Please declare your user agent in request headers:  
> Sample Declared Bot Request Headers:  
> User-Agent: Sample Company Name AdminContact@<sample company domain>.com  
> Accept-Encoding: gzip, deflate  
> Host: www.sec.gov"  
> — *SEC.gov: Accessing EDGAR Data (sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)*

> "Submissions by company and extracted XBRL data are available via RESTful APIs on data.sec.gov, offering JSON formatted data."  
> — *SEC.gov: Accessing EDGAR Data (sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)*
