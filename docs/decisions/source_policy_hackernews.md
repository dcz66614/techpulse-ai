# Source Policy Decision: Hacker News (`hackernews`)

- **Source ID:** `hackernews`
- **Source Family:** `community_discussions`
- **Status:** Approved (Pending final Cd / AUTH-04 sign-off)
- **Enabled:** `true`
- **Verification Timestamp (checked_at):** `2026-10-27T23:00:10Z`
- **Policy Snapshot ID:** `sha256-d05432c450dbf604`
- **Approval Authority:** Cd (`AUTH-04`)

## 1. Official Documentation & Evidence URLs

1. **Official API Repository & Specification:** [https://github.com/HackerNews/API](https://github.com/HackerNews/API) (raw specification: [https://raw.githubusercontent.com/HackerNews/API/master/README.md](https://raw.githubusercontent.com/HackerNews/API/master/README.md))
2. **Robots Exclusion:** [https://news.ycombinator.com/robots.txt](https://news.ycombinator.com/robots.txt)
3. **Hacker News Guidelines:** [https://news.ycombinator.com/newsguidelines.html](https://news.ycombinator.com/newsguidelines.html)
4. **Firebase Endpoint Live Check:** `https://hacker-news.firebaseio.com/v0/item/1.json`

## 2. Allowed Endpoints & HTTP Methods

- **Allowed HTTP Method:** `GET` only.
- **Allowed Base URL:** `https://hacker-news.firebaseio.com/v0/`
- **Allowed Endpoints:**
  - `https://hacker-news.firebaseio.com/v0/item/{id}.json`
  - `https://hacker-news.firebaseio.com/v0/topstories.json`
  - `https://hacker-news.firebaseio.com/v0/newstories.json`
  - `https://hacker-news.firebaseio.com/v0/beststories.json`
  - `https://hacker-news.firebaseio.com/v0/askstories.json`
  - `https://hacker-news.firebaseio.com/v0/showstories.json`
  - `https://hacker-news.firebaseio.com/v0/jobstories.json`
  - `https://hacker-news.firebaseio.com/v0/maxitem.json`
  - `https://hacker-news.firebaseio.com/v0/updates.json`
  - `https://hacker-news.firebaseio.com/v0/user/{username}.json`

## 3. Domain Whitelist

Strict egress allow-list:
- `hacker-news.firebaseio.com`

Direct crawling of `news.ycombinator.com` HTML pages is strictly forbidden. All automated data retrieval must go through `hacker-news.firebaseio.com`.

## 4. Rate Limits & Polite Pool

- **Official Statement on Rate Limits:** "There is currently no rate limit."
- **Self-Imposed Throttle (project choice, not an official limit):**
  - Standard collector pace: 60 requests per minute (1.0 request/second).
  - Maximum burst: 120 requests per minute.
  - Concurrency: Limited to 2 concurrent connections.
  - These values are operational choices made by this project; they are not stated by Hacker News.
- **Data Change Polling:**
  - Poll `/v0/updates.json` or track `/v0/maxitem.json` instead of brute-force polling all item IDs.

## 5. Required Headers & Authentication

- **User-Agent:** Standard polite User-Agent:
  `TechPulse-AI/1.0 (https://github.com/techpulse; contact: info@techpulse.example.com)`
- **Authentication:** None. All endpoints are open public Firebase JSON endpoints.

## 6. Attribution, Caching & Redistribution Rights

- **Attribution:** Attribution to Hacker News / Y Combinator and individual community authors is required.
- **Caching & Retention:** Caching of story metadata, scores, and comment text is permitted and expected.
- **Redistribution Rights:** Public discussion items may be indexed and summarized for tech intelligence trends. Commercial reselling or profiling of personal user data is prohibited.

## 7. Prohibited Behaviors & Error Handling

- **Prohibitions:**
  - Web scraping HTML pages on `news.ycombinator.com` contrary to robots.txt (`Crawl-delay: 30`, disallow actions like `/vote`, `/reply`, `/submitlink`).
  - Flooding Firebase with unthrottled concurrent connections.
- **Error Handling & Rollback:**
  - Gracefully ignore unexpected schema fields: "Clients should gracefully handle additional fields they don't expect, and simply ignore them."
  - On HTTP 5xx or connection timeout: Back off exponentially and retry.
  - On persistent failure: Pause collector, record state, and alert operator.

## 8. Verbatim Supporting Quotes

> "In partnership with Firebase, we're making the public Hacker News data available in near real time."  
> — *Hacker News API Documentation: Overview (github.com/HackerNews/API)*

> "This first iteration will have URIs prefixed with https://hacker-news.firebaseio.com/v0/ and is structured as described below. There is currently no rate limit."  
> — *Hacker News API Documentation: URI and Versioning (github.com/HackerNews/API)*

> "Stories, comments, jobs, Ask HNs and even polls are just items. They're identified by their ids, which are unique integers, and live under /v0/item/<id>.json."  
> — *Hacker News API Documentation: Items (github.com/HackerNews/API)*

> "Clients should gracefully handle additional fields they don't expect, and simply ignore them."  
> — *Hacker News API Documentation: URI and Versioning (github.com/HackerNews/API)*

> "Please email api@ycombinator.com if you find any bugs."  
> — *Hacker News API Documentation: Overview (github.com/HackerNews/API)*
