# Dashboard UI specification

A20 serves a read-only dashboard from `web/src` and leaves the A10 routes unchanged. The page renders payloads from the read API. It does not ship a sample board, a sample trend, or a sample source.

## Files

| Path | Role |
|---|---|
| `web/src/index.html` | Six views, the standing `AI 生成` label, and the `as_of` time |
| `web/src/css/app.css` | Layout for a 375px width and a 1280px width |
| `web/src/js/dashboard.js` | Fetch, publication filter, state selection, and DOM updates |
| `web/tests/` | Static source checks and FastAPI `TestClient` checks |

`create_app` mounts that directory at `/ui` when `web/src` exists. `GET` and `HEAD` read the files. `POST`, `PUT`, `PATCH`, `DELETE`, and `TRACE` receive 405. The OpenAPI path set stays the section 6 routes. If `web/src` is missing, the process skips the mount and the API routes still answer.

## Views

Hash routes on `/ui/`:

| Hash | View | Read |
|---|---|---|
| `#/` | Topic leaderboard | `GET /v1/trends` |
| `#/trends/{id}` | Trend detail | `GET /v1/trends/{id}` |
| `#/trends/{id}/evidence` | Source evidence | trend detail, then `GET /v1/evidence/{id}` for each supporting id |
| `#/trends/{id}/contrary` | Counter-evidence | trend detail, then `GET /v1/evidence/{id}` for each contrary id |
| `#/reports` | Report list | `GET /v1/reports` |
| `#/reports/{id}` | Report download | `GET /v1/reports/{id}` |
| `#/status` | System status | `GET /health/live` and `GET /health/ready` |

The leaderboard sends `topic` and `window` (`7` or `28`) only when the reader sets them. The report list sends `kind` (`daily`, `weekly`, or `topic`) only when the reader sets it. Cursors returned by the API are followed, up to 20 pages, and a repeated cursor stops the loop. The status view does not call `/metrics` or `/docs`.

A report download is a `Blob` of the `content` string from that report response, saved as `{report_id}.md` after the id is reduced to letters, digits, `.`, `_`, and `-`. An empty `content` produces no file.

## States

Each view has `loading`, `empty`, `error`, `degraded`, and `ready`.

| Condition | What is shown |
|---|---|
| Request in flight | `loading`. Previous rows are cleared. |
| Transport failure, HTTP other than 200 or 503, or a body that is not a JSON object | `error`, plus `message` from the body when it is a string. |
| HTTP 200 and no public row | `empty`. |
| HTTP 503, `status: unavailable`, a check equal to `fail`, `stale: true`, `degraded: true`, or a true `*_degraded` flag | `degraded`. |
| HTTP 503 or a degraded flag, and no public row | `degraded` and `empty`. |
| Public rows, including a degraded response that still carries rows | `ready`. A degraded response also keeps the `degraded` line. |

`ready` is filled only from the payload that was just parsed. An empty `items` array, a missing trend, a report with no body and no evidence card, or a status body with no live status and no checks stays on `empty`. The client does not substitute an example topic, score, URL, or report.

Evidence responses with `code: not_found` are skipped. Any other evidence error clears the view and shows `error`.

## Publication

`isPublic` drops a row when `qa_pass` is present and is not `true`, and when `signed` is present and is not `true`. Rows the API already withheld never reach the client. The client has no draft list and no unpublished board.

## Sources

A source URL is rendered only by `sourceLink(row.source_url)` or `sourceLink(card.source_url)`. The anchor uses that URL as `href` and as its text, with `target="_blank"` and `rel="noopener noreferrer"`. The value must start with `https://`. A missing or non-https value is omitted. Source family names stay text. Trend ids link to the evidence and contrary hashes; they are not turned into external URLs.

## Label and time

The header reads `AI 生成` until a payload or row has `generated_by_ai === false`, which switches that label to `非 AI 生成`. Each rendered card repeats the same rule. `as_of` is copied from the payload onto the header `<time datetime>`. A missing `as_of` leaves the time blank. The page does not call `Date.now` or `new Date` to fill it.

## Credential

The optional read token is taken from the password field at request time and sent as `Authorization: Bearer` on `GET` only. The script does not write `localStorage`, `sessionStorage`, `document.cookie`, or `indexedDB`. The field has no default value. Client source contains no token, HMAC key, or private key.

## Layout

`box-sizing: border-box`, `max-width: 100%`, `min-width: 0`, and `overflow-wrap: anywhere` apply to the page text. `pre` uses `white-space: pre-wrap`. The 375px media block keeps the same padding. From 1280px up, the header and main column use `max-width: 72rem` and stay centered. The stylesheet has no `white-space: nowrap`, no `100vw`, and no fixed width that exceeds the narrow viewport.

## Failure

A dashboard failure hides the UI mount or leaves the reader on `error` / `degraded` / `empty`. The read API remains behind its existing auth and rate limit. The client does not gain a write route.
