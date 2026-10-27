# Source Policy Decision: GitHub (`github`)

- **Source ID:** `github`
- **Source Family:** `code_repositories`
- **Status:** Approved (Pending final Cd / AUTH-04 sign-off)
- **Enabled:** `true`
- **Verification Timestamp (checked_at):** `2026-10-27T22:59:40Z`
- **Policy Snapshot ID:** `sha256-261b710f2ed4eeb1`
- **Approval Authority:** Cd (`AUTH-04`)

## 1. Official Documentation & Evidence URLs

1. **REST API Rate Limits:** [https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
2. **Getting Started with the REST API:** [https://docs.github.com/en/rest/using-the-rest-api/getting-started-with-the-rest-api](https://docs.github.com/en/rest/using-the-rest-api/getting-started-with-the-rest-api)
3. **Acceptable Use Policies:** [https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)
4. **Terms of Service:** [https://docs.github.com/en/site-policy/github-terms/github-terms-of-service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service)
5. **Robots Exclusion:** [https://github.com/robots.txt](https://github.com/robots.txt)

## 2. Allowed Endpoints & HTTP Methods

- **Allowed HTTP Method:** `GET` only.
- **Allowed Base URL:** `https://api.github.com`
- **Allowed Endpoints:**
  - `https://api.github.com/repos/{owner}/{repo}`
  - `https://api.github.com/repos/{owner}/{repo}/releases`
  - `https://api.github.com/repos/{owner}/{repo}/tags`
  - `https://api.github.com/search/repositories`
  - `https://api.github.com/rate_limit`

## 3. Domain Whitelist

Strict egress allow-list:
- `api.github.com`

Direct crawling of `github.com` web pages is forbidden. Requests must target `api.github.com`.

## 4. Rate Limits & Polite Pool

- **Primary Rate Limits:**
  - **Unauthenticated requests:** 60 requests per hour (1 request per minute).
  - **Authenticated requests (Personal Access Token / GitHub App):** 5,000 requests per hour (~83 requests per minute).
  - **Search endpoints:** 10 requests per minute unauthenticated, 30 requests per minute authenticated.
- **Secondary Rate Limits:**
  - REST API limit: No more than 900 points per minute.
  - Concurrent requests: Avoid making multiple concurrent requests to the same endpoint.
- **Response Headers Monitored:**
  - `x-ratelimit-limit`: Maximum allowed requests per window.
  - `x-ratelimit-remaining`: Remaining requests in current window.
  - `x-ratelimit-reset`: UTC epoch timestamp when the window resets.
  - `retry-after`: Seconds to wait when secondary rate limit or 429 is encountered.

## 5. Required Headers & Authentication

- **User-Agent Header (Strictly Mandatory):**
  - All API requests MUST include a valid `User-Agent` header.
  - Format: `TechPulse-AI/1.0 (https://github.com/techpulse; contact: info@techpulse.example.com)`
  - *Note:* Requests without a User-Agent header are rejected by GitHub with HTTP 403 Forbidden.
- **API Version & Accept Headers:**
  - `Accept`: `application/vnd.github+json`
  - `X-GitHub-Api-Version`: `2022-11-28`
- **Authentication:**
  - Unauthenticated requests are allowed for public repository metadata at 60 req/hr.
  - Authenticated requests use `Authorization: Bearer <GITHUB_TOKEN>` to unlock 5,000 req/hr.

## 6. Attribution, Caching & Redistribution Rights

- **Caching & Conditional Requests:**
  - Support for `ETag` and `If-None-Match` headers.
  - HTTP `304 Not Modified` responses do not consume primary rate limit quota.
- **Redistribution Rights:**
  - Repository metadata and release activity may be analyzed and retained for tech trend reporting.
  - Code contents remain subject to each specific repository's open-source license.
- **Attribution:**
  - Repository owner and project name must be cited in all evidence cards and reports.

## 7. Prohibited Behaviors & Error Handling

- **Prohibitions:**
  - Missing or invalid User-Agent header (causes 403 Forbidden).
  - Harvesting personal information, email addresses, or user lists for spam or commercial resale.
  - Excessive automated bulk activity placing undue burden on GitHub infrastructure.
  - Automated web scraping of GitHub web pages instead of using the REST API.
- **Error Handling & Rollback:**
  - On primary rate limit exceeded (403/429 with `x-ratelimit-remaining: 0`): Wait until `x-ratelimit-reset`.
  - On secondary rate limit exceeded: Wait for duration specified in `retry-after` header (or minimum 60 seconds).
  - On persistent block: Flag `policy_blocked` and alert operator.

## 8. Verbatim Supporting Quotes

> "All API requests must include a valid User-Agent header. The User-Agent header identifies the user or application that is making the request."  
> — *GitHub REST API Documentation: Getting started (docs.github.com/en/rest/using-the-rest-api/getting-started-with-the-rest-api)*

> "Requests with no User-Agent header will be rejected. If you provide an invalid User-Agent header, you will receive a 403 Forbidden response."  
> — *GitHub REST API Documentation: Getting started (docs.github.com/en/rest/using-the-rest-api/getting-started-with-the-rest-api)*

> "The primary rate limit for unauthenticated requests is 60 requests per hour."  
> — *GitHub REST API Documentation: Rate limits (docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)*

> "You can make unauthenticated requests if you are only fetching public data. Unauthenticated requests are associated with the originating IP address, not with the user or application that made the request."  
> — *GitHub REST API Documentation: Rate limits (docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)*

> "No more than 900 points per minute are allowed for REST API endpoints, and no more than 2,000 points per minute are allowed for the GraphQL API endpoint."  
> — *GitHub REST API Documentation: Secondary rate limits (docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)*

> "If you exceed your primary rate limit, you will receive a 403 or 429 response, and the x-ratelimit-remaining header will be 0. You should not retry your request until after the time specified by the x-ratelimit-reset header."  
> — *GitHub REST API Documentation: Exceeding the rate limit (docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)*

> "If you exceed a secondary rate limit, you will receive a 403 or 429 response and an error message that indicates that you exceeded a secondary rate limit. If the retry-after response header is present, you should not retry your request until after that many seconds has elapsed."  
> — *GitHub REST API Documentation: Exceeding the rate limit (docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)*

> "Scraping refers to extracting information from our Service via an automated process, such as a bot or webcrawler. Scraping does not refer to the collection of information through our API."  
> — *GitHub Acceptable Use Policies (docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)*

> "You may not use information from the Service (whether scraped, collected through our API, or obtained otherwise) for spamming purposes, including for the purposes of sending unsolicited emails to users or selling personal information, such as to recruiters, headhunters, and job boards. Misuse of personal information is prohibited."  
> — *GitHub Acceptable Use Policies (docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)*
