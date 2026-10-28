# Runbook

Operational procedures are added as deployment and operations tickets are completed.

## O20 Local scheduling

This process has no Temporal server. `LocalOrchestrator` in `src/techpulse/workflows/orchestrator.py` is the in-process runtime. `WorkflowRuntime` is the small interface a Temporal worker can implement later. The `temporalio` package is not a dependency, and this module does not open a socket.

### Schedules

- `techpulse-daily-v1` fires at 08:00 UTC.
- `techpulse-weekly-v1` fires on Monday at 09:00 UTC.
- Both flows chain the same steps: `C10`, `C11`, `C12`, `C20`, `C21`, then `D10`, `D20`, `D21`, `D30`, `D40`, `I10`, `I20`, `T10`, `T20`, `T30`, `E10`, `E20`, `R10`, `Q10`, then the publish gate.
- Collectors have no edges between them. The local runtime runs a ready step to completion before the next one. The caps are collectors 5, cleaners 3, inference 2, deployment 1.
- Each step names an existing callable in `PIPELINE_BINDINGS`. The default activity calls `archive_collector_ndjson` and `deduplicate_records` only when local inputs are already in the activity params. Collectors are not started without a local fixture. `C21` also names `RSSCollector`, which stays blocked until a feed is approved.

### Attempts, timeout, and backoff

- An activity timeout is 3600 seconds unless a caller passes a shorter one.
- A task gets 3 attempts in total (the first try plus two retries).
- HTTP 429 waits `30 * 2^(attempt-1)` seconds, and at least `Retry-After` when that value is positive.
- Other retryable failures wait 30 seconds, then 120 seconds.
- `fatal_error`, `schema_incompatible`, and `permission_violation` are not retried.
- HTTP 401, HTTP 403, captcha, and a terms block are `policy_blocked` and are not retried.
- Pass a clock whose `sleep` and `advance` are under the test's control. `SystemClock.advance` refuses so a simulated day cannot sleep on the wall clock.
- Jitter is a callable `(attempt, task_id) -> seconds`. The default returns 0 so a replay stays deterministic.

### policy_blocked

`policy_blocked` sets `source_registry.enabled` to 0 before the next step runs. A later collector for that source is not called. Other sources continue. A collector that ends in `fatal_error` or an exhausted retry does not block `D10`. A failure of a later shared step leaves its dependents `waiting`.

### Checkpoints, DLQ, and replay

The orchestrator SQLite file holds:

- `o20_task_state` and `o20_checkpoints` for the current status and each change
- `o20_audit` as append-only status rows
- `o20_dlq` for `fatal_error`, `policy_blocked`, and retryable work that used all 3 attempts
- `o20_results` keyed by `(task_id, input_sha256)`
- `o20_records` for rows inserted by a successful attempt
- `o20_fires` for a slot's stable `run_id` and payload
- `published_reports` for the staging promotion queue
- `o20_budget` for the UTC day's spend
- `o20_control` for the pause flag

Before an activity is called, the runtime hashes the payload and the upstream outputs and looks up `input_sha256`. A stored terminal result is reused. The activity is not called, and no record row is inserted. The same payload under a new `run_id` follows that lookup. A scheduled slot keeps the `run_id` from its first fire.

Task status moves only along `waiting → ready → running → success | retryable_error | fatal_error | policy_blocked`. `retryable_error` may return to `ready` while an attempt remains. `success`, `fatal_error`, and `policy_blocked` stop. An illegal move raises `IllegalTransition` and writes nothing.

### Publish

The publish gate runs only after `Q10` is `success`. It inserts one `published_reports` row only when the stored `qa_pass` value is boolean true. Any other value, including false, leaves the queue unchanged. Production publication still needs the later gates (Q20, V90, AUTH-06). This queue is the staging promotion from section 12.1.

### Budget

A task result above $5 is `fatal_error` and is not retried. When the UTC day's spend is already $25, the runtime pauses and does not start another activity.

The O30 budget hook on the same SQLite file records that spend. At $20 (80% of the $25 cap) the mode is `degraded` and non-essential tier-2 model work is off. At $25 the circuit breaker is open and this runtime pauses. The hook cannot be turned off. If the metrics rows cannot be read, non-P0 LLM calls are held and ingestion still runs. Alert handling is in `docs/ops_slo.md`.

### Scheduler fault

An unexpected exception from an activity pauses the scheduler. The task stays at its last checkpoint (`running` if the attempt had started). New slots do not start. The queue and the audit rows stay. After `resume()`, the same slot continues. A `running` task with no stored result runs that attempt again. Completed tasks are skipped by `input_sha256`.

### What this does not do

- It does not connect to Temporal, Postgres, S3, or a third-party API.
- It does not delete audit rows.
- It does not publish a report that failed Q10.
