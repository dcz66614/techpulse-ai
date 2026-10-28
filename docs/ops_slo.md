# Operations SLOs and alert SOPs

The daily LLM cap is $25. The budgeter degrades at $20 (80%) and opens the circuit breaker at $25 (100%). Non-essential tier-2 model work turns off at the degrade line. At the breaker, non-P0 LLM calls stop. Ingestion, alerts, and security checks stay up.

The budgeter cannot be turned off. If the metrics store cannot be read, non-P0 LLM calls pause. That is fail-closed. A missing figure is not treated as zero spend for those calls.

The only configured model is `local/template`. No paid official API is routed. A billable model stays off until a pricing snapshot exists and the user approves it. This file does not grant that approval.

Each alert below stores the `run_id` and a link to one SOP on this page.

## P0 failure

Trigger: a P0 task ends in `fatal_error`, or the daily or weekly run cannot finish a P0 step.

Automatic action: keep the audit row, open this alert, and leave the circuit breaker in force if the day's spend is already $25. Do not raise the cap. Do not switch the route to a paid API.

Human steps:

1. Open the alert and copy `run_id`.
2. Read the task checkpoint and the O30 log for that `run_id`.
3. Fix the failing step. Replay uses the stored idempotency key.
4. Close the alert only after the same step succeeds on a later run, or the user accepts the failure.

The SOP link stored on the alert is `docs/ops_slo.md#p0-failure`.

## Source policy change

Trigger: a source policy snapshot id changes, or a collector returns `policy_blocked` (HTTP 401, HTTP 403, captcha, or a terms block).

Automatic action: disable that source before the next collector step. Other approved sources continue. Open this alert with the `run_id`. Do not bypass the block.

Human steps:

1. Open the alert and copy `run_id` and the source id from the summary.
2. Compare the new policy snapshot with the approved registry entry.
3. Leave the source disabled until the user approves the change.
4. A registry edit still follows the source-policy decision records. This alert does not approve one.

The SOP link stored on the alert is `docs/ops_slo.md#source-policy-change`.

## Stale evidence

Trigger: the evidence behind a published or staged report is older than its window, or the previous run is serving with `stale=true`.

Automatic action: keep the last good report in service, mark `stale=true`, and set `as_of` from the last good run. Open this alert with the `run_id` of the run that detected the stale set. Do not invent a fresher citation.

Human steps:

1. Open the alert and copy `run_id`.
2. Compare `as_of` with the report window.
3. Re-run the evidence step when the sources are healthy.
4. Leave the stale flag until a new Q10 pass replaces that snapshot.

The SOP link stored on the alert is `docs/ops_slo.md#stale-evidence`.
