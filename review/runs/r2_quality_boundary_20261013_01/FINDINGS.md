# Signal quality boundary closure

Original R2 literal signal values, intervals, PET/unit/rank/AI results and earlier archives remain unchanged. Ten source intervals receive SOURCE_QUALITY_UNRESOLVED_LONG_INTERVAL over their exact group-specific integer [start,end) bounds. This is conservative quality withholding, not confirmation of a signal error.

|Original-time population|New tail unique events|Tail role queries|Total held events including original dates|
|---|---:|---:|---:|
|ALL_LEDGER_EVENTS|7101|14194|42418|
|VALID_NONNEGATIVE_PET|7062|14116|42259|
|SHORT_NONNEGATIVE_PET_LE_1S|49|98|267|

All nine offsets keep the same original population denominators. These tables measure quality membership only; no new signal-state sensitivity or model is computed. Role counts must not be used as event denominators.

The literal PET<=1 total is 4492/924775. The disjoint partition is quality-held 267/42259, remaining UNKNOWN/ambiguous 5/1250, and interpretable descriptive context 4220/881266. Cell-level fractions and zero-denominator nulls are in analysis/signal_condition_quality.csv.

Pre-existing withheld_signal_conclusion, sample_status and observed-date columns remain ORIGINAL literal R2 fields. Use the new quality_held_* and interpretable_* columns for this closure; the preserved old withheld flag is not the updated quality gate. No new interpretable-subset date count or sample-stability claim is made.

Full-period preserved ranks retain 3A/3B as the top two across A/B/C/D and four recorded thresholds, while exact ordering changes. This is not all-condition stability or an accident-risk ranking.

Validation: 15 boundary tests plus 8 provenance/literal-CSV preservation tests; two new overlay runs with seven byte-identical artifacts. Original 480 input/artifact entries, 32 R2 outputs and 68 R2 review members were hash-checked in each run. Existing 139 source files stayed unchanged. Actual model fits 52 before and after; additional fits 0. No baseline or full R2 rerun.

The initial inspection attempt stopped on string timestamp comparison before writing results; its source was preserved, explicit integer parsing was applied, and the completed inspection was independently matched to final counts.

Previous technical ZIPs/releases remain the earlier R2 version. This boundary closure is supplied through this new Git review run and its new source commit; no old ZIP/tag was replaced.
