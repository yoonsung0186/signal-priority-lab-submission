# R2 signal-quality boundary closure

This additive overlay preserves the completed R2 run `r2_finish_20261013_01`
and literal source `dd83988e74696603b515f1944766207b2d3d26cf`. It does not edit
`r2_signals.py`, provider signal states, interval endpoints, PET, observation
units, period ranks, AI models, fit logs, or earlier releases.

## Membership and interpretation

Read the exact integer millisecond endpoints and signal group IDs from the ten
rows of R2 `signal_missing_period_crossings.csv`. Independently match these
rows to the preserved read-only signal cache. For each own-role entry query,
membership is `matching_group AND start <= original_entry_ms + delta < end`.
Mark the entire interval `SOURCE_QUALITY_UNRESOLVED_LONG_INTERVAL`. This is a
conservative source-quality reservation, not confirmation of a signal error.
No ffill, interpolation, deletion, or replacement with invented signal states
is performed.

The previous date reservation is retained: either ORIGINAL role entry within
the local June 5–11 candidate window. The CSV's common candidate start/end
integers encode that local-date window. New event-level reservation is the
union of this fixed original reservation and either role's long-interval hit.
Role queries and unique events are counted separately; two affected roles
still represent one event. Negative-PET events remain in the ALL population.

`boundary_impact.csv` and `boundary_role_impact.csv` evaluate the existing nine
offsets on the same original event cohort, with fixed ALL/nonnegative/short-PET
population denominators. Only event query times move. These are quality-mask
sensitivity counts, not newly computed state-change rates or an empirical
clock-error distribution. Existing R2 state-change results remain literal.

At offset zero, `signal_condition_quality.csv` retains all original literal
condition-table columns and adds a disjoint partition, with quality taking
precedence over UNKNOWN/ambiguous state:

`N_literal = N_quality + N_unknown_remaining + N_interpretable`

The same identity applies to the short-PET numerator at each original tau.
The interpretable fraction is `n_interpretable / N_interpretable`, or null when
the denominator is zero. "Interpretable" means only that this particular
quality reservation and literal UNKNOWN/ambiguity do not block descriptive
signal context; it is not independently validated signal truth or risk.

Old candidate cell counts are reused and checked against original event
membership. Only newly reserved nonnegative events require state lookup to
allocate the additional reservation to literal cells. The two reservation
sets are disjoint. No whole baseline or R2 recalculation is needed.

## Reproduction and preservation

Use the existing project environment and `requirements-r2.lock`, original
licensed baseline shards/cache, and hash-bound R2 review artifacts identified
by `configs/r2_signal_quality.json`. Run:

```text
.venv/Scripts/python.exe -m unittest discover -s tests -p test_r2_signal_quality.py -v
.venv/Scripts/python.exe tools/r2_signal_quality.py --out runs/<fresh_quality_id_1>
.venv/Scripts/python.exe tools/r2_signal_quality.py --out runs/<fresh_quality_id_2>
```

Set `PYTHONPATH=src` and `PYTHONUTF8=1` for the test command. Compare all output
hashes. Existing output directories are rejected. Source/config/input identity,
all preserved baseline and R2 artifact hashes, and unchanged cumulative fit
counts are verified. New fit budget is zero; no original budget is reset.
Actual command logs and replay comparisons are retained with the new review.

## Narrow rank clarification, reused without recomputation

The preserved full-period R2 unit table has scenarios 3A and 3B in the top two
for A/B/C/D at all four recorded PET thresholds (0.5, 1, 1.5, 2 seconds).
Their exact order is not invariant: at tau=1, A/B order is 3B then 3A, whereas
C/D order is 3A then 3B. This describes that table only. It does not establish
accident-risk priority, monthly/bootstrap stability, signal-conditioned rank
stability, or validity of the source-quality-reserved interval.

Provider paper precision/missing-period interpretation, independent geometry,
and human ground truth remain unresolved. This closure creates no report,
policy narrative, model exploration, public sharing, or contest submission.
