# R2 derived table dictionary

These tables describe fixed SIT-PET v1.1 recorded interactions. They are not traffic-exposure rates, accident probabilities, ground-truth accuracy or violation labels. Empty CSV numeric cells represent undefined/null values, never zero by implication. Source definitions and units are also recorded in `units_metadata.json`, `signals_metadata.json`, `metric_facts.jsonl` and `figures_index.json`.

|Table / field|Meaning and unit|
|---|---|
|unit_summary / unit|A all QC-valid nonnegative events; B A restricted to PET ≤ 5 s; C first signed QC-valid event per scenario and ordered pair; D select C first, then restrict to 0–5 s.|
|n_raw|Source rows represented by the input ledger within the period, before QC and cohort selection.|
|n_pairs|Distinct scenario + encroaching object + priority object tuples within the selected cohort; roles are ordered.|
|n_selected|Selected recorded events before excluding negative C representatives from the fraction denominator.|
|n_negative|Selected events with PET < 0; C retains and reports these instead of replacing them with later nonnegative events.|
|n_negative_initial_eligible|Signed QC-valid negative events before cohort filtering; separate from n_negative.|
|numerator, denominator, fraction|Selected valid 0 ≤ PET ≤ tau events / selected valid PET ≥ 0 events; conditional fraction in [0,1], null when denominator is zero.|
|tau_s, pet_s|Threshold and provided signed PET, in seconds. Zero PET is retained.|
|rank, tie_count|Descending conditional-fraction rank and exact-ratio tie count. ALL is not ranked; unsupported/zero-denominator cases stay null.|
|original_A_rank, rank_change_from_A|Same-scenario/tau A point rank and current rank minus it. Lower numerical rank means larger fraction, not proven policy priority.|
|monthly_rank_sensitivity, leave_one_month_rank_sensitivity|Slice globally fixed A/B/C/D cohorts by local month or excluding that month; representatives are not selected again.|
|pair_multiplicity|Repeated events per ordered pair and distinct provider conflict-coordinate counts. Coordinate variation is not independently established geometric-zone multiplicity.|
|unit_selection|Local event-level selection flags and representative ordering timestamp (minimum of four original UTC millisecond times). Large file omitted from distributed packages, with local SHA and size preserved.|
|bootstrap_date_resamples|Replicate, draw position and sampled observed-local-date index. B=200, seed=20261012; the same indices are shared across all units/scenarios/thresholds.|
|bootstrap_rank_replicates, bootstrap_rank_summary|Date-weighted n/N, exploratory ranks, ties, valid and zero-denominator repetitions, min/max/quantiles. Rank eligibility requires 30 events and 7 observed dates. Quantiles are not population/generalization confidence bounds.|
|signal_coverage_daily / signal_group_id, local_date|One used signal group × complete Europe/Vienna calendar day; UTC millisecond bounds reflect DST.|
|day_seconds|Actual local-day duration, in seconds (not always 86,400).|
|gap_seconds, overlap_seconds, observed_unknown_seconds|Uncovered, multiply covered, and explicitly unknown single-state interval durations. Units are seconds per group/day; sums across groups are group-seconds.|
|effective_known_seconds, effective_unknown_or_ambiguous_seconds|Exclusive partition of the complete day after conservative source-QC handling; sum equals day_seconds.|
|green/yellow/red/red_yellow_covered_seconds|Exclusive known-state durations after overlap and invalid-context treatment.|
|longest_original_interval_seconds|Full source interval duration even if it extends beyond this day; distinct from clipped duration.|
|signal_missing_period_crossings|Source intervals crossing the candidate 2025-06-05~11 period; covering that period in a file does not prove continuous observation.|
|signal_event_coverage_daily / n_events|Role-entry queries, grouped by signal group/date/role. Each ledger event contributes two role queries; this denominator is not event count.|
|n_known_signal, n_unknown_signal, n_ambiguous_signal|Role queries with known state, unresolved/unknown/gap state, or overlapping states.|
|signal_event_changes / population|ALL_LEDGER_EVENTS, VALID_NONNEGATIVE_PET, or SHORT_NONNEGATIVE_PET_LE_1S. The population remains fixed at every offset.|
|clock_offset_s|Stress shift applied equally to all four event timestamps; signal clock, PET and event key remain fixed.|
|n_pair_state_changed / pair_state_change_rate|Same-event ordered role-state pair differs from offset zero / population event count. Each role uses its own entry time, not a common simultaneous time.|
|signal_state_transitions|Counts by population/scenario/offset/role/from_state/to_state; PAIR uses ordered state pairs. UNKNOWN and SIGNAL_AMBIGUOUS remain separate.|
|signal_pair_tv / aggregate_role_pair_tv|Half the L1 distance of aggregate pair-state distributions, divided by event count. Zero TV does not guarantee individual-event invariance.|
|signal_condition_pet|Scenario × encroaching-entry state × priority-entry state × tau at offset zero. Each row includes short-PET numerator, valid-nonnegative denominator, observed dates and sample status.|
|candidate_period_n_valid_nonnegative_pet, candidate_period_n_short_pet|Same conditional cell restricted to events for which either role entry falls in the unresolved candidate period. Retained descriptively, signal conclusions withheld.|
|sample_status, withheld_signal_conclusion|Low counts (<30) / empty cells and unresolved source quality are explicit. These fields do not certify representativeness.|
|inspection_candidates_r2|Definitions, original A n/N, observation-unit/month/leave-month/bootstrap rank ranges, signal-state stress change and unresolved geometry/source factors. Unstable candidates are marked for additional observation.|

The frozen 100,000-event AI sample has its own denominator. Its 97.88% static-dimension centroid-separation share is neither accuracy nor explained accident risk. Existing automated numeric review of 8,699 events is preserved separately from unperformed human truth/geometry review.
