# Source Policy Decision: Generic RSS Feeds (`rss`)

- **Source ID:** `rss`
- **Source Family:** `syndicated_feeds`
- **Status:** Disabled (Phase 2 Source; decentralized specification without single endpoint / ToS)
- **Enabled:** `false`
- **Disable Reason:** Phase 2 source; must stay enabled: false per project execution plan; RSS/Atom is a decentralized format specification (RSS 2.0 / RFC 4287) without a centralized provider, fixed domain, or universal ToS; requires per-feed domain whitelisting and compliance review.
- **Verification Timestamp (checked_at):** `2026-10-27T23:02:40Z`
- **Policy Snapshot ID:** `sha256-1610f254e9312593`
- **Approval Authority:** Cd (`AUTH-04`)

## 1. Official Documentation & Evidence URLs

1. **RSS 2.0 Specification:** [https://www.rssboard.org/rss-specification](https://www.rssboard.org/rss-specification)
2. **Atom Syndication Format Specification (RFC 4287):** [https://www.rfc-editor.org/rfc/rfc4287](https://www.rfc-editor.org/rfc/rfc4287)
3. **RSS Board Robots Exclusion:** [https://www.rssboard.org/robots.txt](https://www.rssboard.org/robots.txt)
4. **W3C Feed Validation Standards:** [https://validator.w3.org/feed/](https://validator.w3.org/feed/)

## 2. Allowed Endpoints & HTTP Methods

- **Allowed HTTP Method:** `GET` only (conditional GET with `If-Modified-Since` and `If-None-Match`).
- **Allowed Base URL:** `not stated` (Decentralized protocol; no single provider exists).
- **Allowed Endpoints:** `not stated` (Must be evaluated on a per-feed basis upon Phase 2 rollout).

## 3. Domain Whitelist

Strict egress allow-list:
- `[]` (Empty by default)

Because RSS is a decentralized format, each prospective feed source (e.g., individual engineering blogs, release feeds) must have its domain individually vetted and whitelisted by Cd (`AUTH-04`) before enabling.

## 4. Rate Limits & Polite Pool

- **Requests per Second:** `not stated` (Publisher-dependent).
- **Requests per Minute:** `not stated` (Governed per-feed by channel `<ttl>` metadata).
- **Polite Pool Rules:**
  - Aggregators must strictly honor the `<ttl>` (time to live) element specifying the cache lifetime in minutes.
  - Aggregators must respect `<skipHours>` and `<skipDays>` hints specifying times when the feed must not be queried.
  - Collectors must send HTTP conditional headers (`If-Modified-Since`, `If-None-Match`) to minimize server bandwidth on unchanged feeds (`304 Not Modified`).

## 5. Required Headers & Authentication

- **User-Agent:** Polite RSS feed aggregator User-Agent identifying the reader and contact URL:
  `TechPulse-AI/1.0 (https://github.com/techpulse; contact: info@techpulse.example.com)`
- **Conditional Headers:**
  - `If-Modified-Since`
  - `If-None-Match`
- **Authentication:** None for open public RSS feeds; individual publishers may vary.

## 6. Attribution, Caching & Redistribution Rights

- **Attribution:** Mandatory per channel `<copyright>` and `<title>` elements: "Copyright notice for content in the channel."
- **Caching & Retention:** Caching is explicitly governed by the `<ttl>` element: "ttl stands for time to live. It's a number of minutes that indicates how long a channel can be cached before refreshing from the source."
- **Redistribution Rights:** RSS feeds provide syndication of story headlines and descriptions. Full-text republication is not granted by default under the RSS specification and is subject to the copyright of individual publishers.

## 7. Prohibited Behaviors & Error Handling

- **Prohibitions:**
  - Querying feeds faster than the duration declared in `<ttl>`.
  - Ignoring `<skipHours>` and `<skipDays>` directives.
  - Disregarding publisher robots.txt rules or anti-crawling directives.
- **Policy Enforcement:** Remains `enabled: false`. Phase 2 implementation requires configuring individual feed URLs with explicit publisher domain whitelisting and Cd (`AUTH-04`) approval.

## 8. Verbatim Supporting Quotes

> "RSS is a Web content syndication format. Subordinate to the <rss> element is a single <channel> element, which contains information about the channel (metadata) and its contents."  
> — *RSS 2.0 Specification (rssboard.org/rss-specification)*

> "ttl stands for time to live. It's a number of minutes that indicates how long a channel can be cached before refreshing from the source."  
> — *RSS 2.0 Specification: <ttl> sub-element of <channel> (rssboard.org/rss-specification)*

> "skipHours: A hint for aggregators telling them which hours they can skip. This element contains up to 24 <hour> sub-elements whose value is a number between 0 and 23, representing a time in GMT, when aggregators, if they support the feature, may not read the channel on hours listed in the <skipHours> element."  
> — *RSS 2.0 Specification: <skipHours> (rssboard.org/rss-specification)*

> "skipDays: A hint for aggregators telling them which days they can skip. This element contains up to seven <day> sub-elements whose value is Monday, Tuesday, Wednesday, Thursday, Friday, Saturday or Sunday. Aggregators may not read the channel during days listed in the <skipDays> element."  
> — *RSS 2.0 Specification: <skipDays> (rssboard.org/rss-specification)*

> "copyright: Copyright notice for content in the channel."  
> — *RSS 2.0 Specification: Optional channel elements (rssboard.org/rss-specification)*
