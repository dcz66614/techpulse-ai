# T30 score weights

Status: engineering default for ticket T30. These coefficients are the initial
weights in section 7.3 of the execution plan. They are not taken from the
research report and they have not been backtested. Offline evaluation may
replace them later. Until that happens, scoring uses this file's formula.

## Formula

Each feature is clipped to [0, 1]. `anomaly_penalty` is clipped to [0, 0.30].

```text
score = 0.25 * normalized_growth_7d
      + 0.20 * cross_source_diversity
      + 0.20 * source_quality
      + 0.15 * novelty
      + 0.10 * persistence_28d
      + 0.10 * developer_activity
      - anomaly_penalty
```

The number is computed only from fields stored on the T20 feature row
(the six components, the penalty, and the unknown flags). A later run with
the same stored row returns the same score. Unknown components contribute 0
and force an experimental score. They are not filled in.

## What the features measure

T20 defines the inputs. Windows are UTC calendar days.

- `normalized_growth_7d`: clipped `(current - previous) / previous` for
  independent records. It is null when the previous window has fewer than 3
  records. The numerator and denominator are stored either way.
- `cross_source_diversity`: distinct source families in the 7-day window
  divided by 4.
- `source_quality`: mean tier weight of independent 7-day records
  (A = 1, B = 0.5, C = 0). Null when no tier is present.
- `novelty`: share of 7-day independent keys that do not appear before the
  window starts.
- `persistence_28d`: UTC days in the 28-day window that contain an
  independent record, divided by 28.
- `developer_activity`: share of independent 7-day records in the code family.
- `anomaly_penalty`: reprint ratio, same-domain multi-host counts, bot-like
  concentration, and a small previous window. Each reason stores the raw
  metric it used. The sum is capped at 0.30.

## Confidence

`high` requires all of the following: at least three source families, at least
two Tier-A evidence ids, `citation_chain_complete`, `contrary_search_executed`,
and `qa_pass`. One source family is never `high`. A score that misses a gate
is `medium` when at least two families are present, and `low` otherwise.

If any scored component is unknown, or no calibration record was supplied,
the row is an `experimental_score` and confidence is `low`. A non-finite
component or a penalty with no raw metric is `fatal_error`. No trend row is
written in that case.

Both the 7-day and 28-day trend rows use this formula. `window_days` says
which window the claim describes. The weights do not change between them.
