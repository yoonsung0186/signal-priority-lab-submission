"""No-fit observation-unit and period sensitivity over an immutable PET ledger.

A/B retain event observations. C/D are explicitly alternative units, never
corrections to A. First representatives are chosen from QC-valid signed PET
before removing negatives or applying the 5-second filter. All period analyses
slice these already fixed cohorts; they do not choose a replacement event.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np
import pandas as pd

TIMES = ('ts_enter_encroaching_ms', 'ts_leave_encroaching_ms', 'ts_enter_priority_ms', 'ts_leave_priority_ms')
UNITS = ('A', 'B', 'C', 'D')
UNIT_DEFINITIONS = {
    'A': 'All QC-valid nonnegative provider events',
    'B': 'All QC-valid provider events with 0 <= PET <= 5 seconds',
    'C': 'First QC-valid signed event per scenario and ordered object pair; denominator then excludes negative representatives',
    'D': 'Choose C first, then retain representatives with 0 <= PET <= 5 seconds',
}
REPRESENTATIVE_ORDER = ('minimum_of_four_UTC_timestamps_ms_ASC', 'numeric_event_id_ASC', 'event_key_ASC')
REQUIRED = ('event_key', 'event_id', 'scenario_id', 'encroaching_object_id', 'priority_object_id',
            'pet_s', 'qc_status', 'event_date_local', *TIMES)
MIN_BOOTSTRAP_EVENTS = 30
MIN_BOOTSTRAP_DATES = 7


def _boolean(series: pd.Series) -> pd.Series:
    parsed = series.map({True: True, False: False, 'True': True, 'False': False, 'true': True, 'false': False})
    if parsed.isna().any():
        raise ValueError('R2_UNRECOGNIZED_BOOLEAN')
    return parsed.astype(bool)


def prepare_ledger(ledger: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Validate unique events; repeated ordered pairs are legitimate observations."""
    missing = sorted(set(REQUIRED) - set(ledger.columns))
    if missing:
        raise ValueError('R2_LEDGER_COLUMNS_MISSING: ' + ','.join(missing))
    columns = list(REQUIRED) + [name for name in ('n_source_rows', 'pet_eligible', 'conflict_lon_deg', 'conflict_lat_deg') if name in ledger]
    frame = ledger.loc[:, columns].copy()
    if frame['event_key'].isna().any() or frame['event_key'].astype(str).str.strip().eq('').any() or frame['event_key'].duplicated().any():
        raise ValueError('R2_EVENT_KEYS_MUST_BE_UNIQUE_AND_PRESENT')
    frame['event_key'] = frame['event_key'].astype(str)
    if frame['event_key'].duplicated().any():
        raise ValueError('R2_EVENT_KEYS_MUST_BE_UNIQUE_AND_PRESENT')
    frame['scenario_id'] = frame['scenario_id'].fillna('UNKNOWN_SCENARIO').astype(str)
    if frame['scenario_id'].eq('ALL').any():
        raise ValueError('R2_SCENARIO_ALL_RESERVED_FOR_AGGREGATE')
    frame['pet_s'] = pd.to_numeric(frame['pet_s'], errors='coerce')
    if not frame['qc_status'].isin(['VALID_NONNEGATIVE_PET', 'NEGATIVE_PET_REVIEW', 'INVALID']).all():
        raise ValueError('R2_UNRECOGNIZED_QC_STATUS')
    eligible = frame['qc_status'].isin(['VALID_NONNEGATIVE_PET', 'NEGATIVE_PET_REVIEW'])
    if 'pet_eligible' in frame and not _boolean(frame['pet_eligible']).equals(eligible):
        raise ValueError('R2_PET_ELIGIBILITY_STATUS_CONTRADICTION')
    if (~np.isfinite(frame.loc[eligible, 'pet_s'].to_numpy(dtype=float))).any():
        raise ValueError('R2_QC_VALID_NONFINITE_PET')
    if ((frame['qc_status'].eq('VALID_NONNEGATIVE_PET') & frame['pet_s'].lt(0)) |
        (frame['qc_status'].eq('NEGATIVE_PET_REVIEW') & frame['pet_s'].ge(0))).any():
        raise ValueError('R2_SIGN_STATUS_CONTRADICTION')
    source_counts = pd.to_numeric(frame['n_source_rows'], errors='coerce') if 'n_source_rows' in frame else pd.Series(1, index=frame.index)
    if source_counts.isna().any() or (source_counts < 1).any() or (source_counts % 1 != 0).any():
        raise ValueError('R2_SOURCE_ROW_COUNTS_INVALID')
    frame['n_source_rows'] = source_counts.astype('int64')
    dates = pd.to_datetime(frame['event_date_local'], format='%Y-%m-%d', errors='coerce')
    frame['_date'] = dates.dt.strftime('%Y-%m-%d')
    frame['_month'] = dates.dt.strftime('%Y-%m')
    frame['_eligible'] = eligible
    for column in TIMES:
        numeric = pd.to_numeric(frame[column], errors='coerce')
        valid = numeric.loc[eligible]
        if valid.isna().any() or not np.isfinite(valid.to_numpy(dtype=float)).all() or (valid % 1 != 0).any():
            raise ValueError('R2_QC_VALID_INVALID_TIMESTAMP')
        frame[column] = numeric
    frame['_representative_time_ms'] = frame[list(TIMES)].min(axis=1)
    numeric_ids = pd.to_numeric(frame['event_id'], errors='coerce')
    valid_ids = numeric_ids.loc[eligible]
    if valid_ids.isna().any() or (valid_ids < 0).any() or (valid_ids % 1 != 0).any():
        raise ValueError('R2_QC_VALID_INVALID_EVENT_ID')
    frame['_numeric_event_id'] = numeric_ids
    for column in ('encroaching_object_id', 'priority_object_id'):
        if frame.loc[eligible, column].isna().any() or frame.loc[eligible, column].astype(str).str.strip().eq('').any():
            raise ValueError('R2_QC_VALID_MISSING_ORDERED_PAIR')
        frame[column] = frame[column].astype('string')
    signed = frame.loc[eligible].copy()
    ordered = signed.sort_values(['_representative_time_ms', '_numeric_event_id', 'event_key'], kind='stable')
    first = ordered.drop_duplicates(['scenario_id', 'encroaching_object_id', 'priority_object_id'], keep='first')
    frame['_first_representative'] = frame['event_key'].isin(first['event_key'])
    frame['_A'] = eligible & frame['pet_s'].ge(0)
    frame['_B'] = frame['_A'] & frame['pet_s'].le(5)
    frame['_C'] = frame['_first_representative']
    frame['_D'] = frame['_C'] & frame['pet_s'].between(0, 5, inclusive='both')
    return frame, first


def _ranks(rows: list[dict[str, Any]], eligible_scenarios: set[str] | None = None) -> None:
    """Exact integer cross-products: competition rank, equal fractions tie."""
    supported = [row for row in rows if row['scenario_id'] != 'ALL' and row['denominator'] > 0 and
                 (eligible_scenarios is None or row['scenario_id'] in eligible_scenarios)]
    for row in rows:
        if row not in supported:
            row.update(rank=None, tie_count=None, rank_reason='ALL_AGGREGATE_NOT_RANKED' if row['scenario_id'] == 'ALL' else
                       'INSUFFICIENT_BASELINE_BOOTSTRAP_SUPPORT' if row['denominator'] > 0 and eligible_scenarios is not None else 'ZERO_DENOMINATOR')
            continue
        products = [(other['numerator'] * row['denominator'], row['numerator'] * other['denominator']) for other in supported]
        row.update(rank=1 + sum(left > right for left, right in products), tie_count=sum(left == right for left, right in products),
                   rank_reason='DESCENDING_CONDITIONAL_FRACTION_COMPETITION_RANK_WITH_TIES_NOT_POLICY_PRIORITY')


def _point_rows(frame: pd.DataFrame, scenarios: list[str], thresholds: tuple[float, ...], period: str, period_value: str,
                scope_mask: pd.Series) -> list[dict[str, Any]]:
    output = []
    base = frame.loc[scope_mask]
    raw = base.groupby('scenario_id', observed=True)['n_source_rows'].sum().to_dict()
    raw_unique = base.groupby('scenario_id', observed=True).size().to_dict()
    signed = base.loc[base['_eligible']]
    initial_negative = signed.loc[signed['pet_s'].lt(0)].groupby('scenario_id', observed=True).size().to_dict()
    first_negative = base.loc[base['_C'] & base['pet_s'].lt(0)].groupby('scenario_id', observed=True).size().to_dict()
    initial_pairs = signed[['scenario_id', 'encroaching_object_id', 'priority_object_id']].drop_duplicates().groupby('scenario_id', observed=True).size().to_dict()
    for unit in UNITS:
        selected = base.loc[base['_' + unit]]
        groups = {scenario: group for scenario, group in selected.groupby('scenario_id', sort=True, observed=True)}
        groups['ALL'] = selected
        for tau in thresholds:
            ranked = []
            for scenario in [*scenarios, 'ALL']:
                group = groups.get(scenario, selected.iloc[:0])
                nonnegative = group.loc[group['pet_s'].ge(0)]
                numerator = int(nonnegative['pet_s'].le(tau).sum()); denominator = len(nonnegative)
                pair_count = len(group[['scenario_id', 'encroaching_object_id', 'priority_object_id']].drop_duplicates())
                row = {'unit': unit, 'scenario_id': scenario, 'period_type': period, 'period_value': period_value, 'tau_s': tau,
                       'n_raw': int(sum(raw.values()) if scenario == 'ALL' else raw.get(scenario, 0)),
                       'n_unique_events_before_selection': int(sum(raw_unique.values()) if scenario == 'ALL' else raw_unique.get(scenario, 0)),
                       'n_pairs': pair_count, 'n_selected': len(group), 'n_negative': int(group['pet_s'].lt(0).sum()),
                       'n_pairs_initial_eligible': int(sum(initial_pairs.values()) if scenario == 'ALL' else initial_pairs.get(scenario, 0)),
                       'n_negative_initial_eligible': int(sum(initial_negative.values()) if scenario == 'ALL' else initial_negative.get(scenario, 0)),
                       'n_negative_first_representatives_before_filter': int(sum(first_negative.values()) if scenario == 'ALL' else first_negative.get(scenario, 0)),
                       'numerator': numerator, 'denominator': denominator, 'fraction': numerator / denominator if denominator else None,
                       'n_observed_local_dates': int(nonnegative['_date'].nunique()), 'n_missing_local_date': int(nonnegative['_date'].isna().sum()),
                       'filter': UNIT_DEFINITIONS[unit], 'unit_scope': 'ORIGINAL_EVENT_BASELINE' if unit == 'A' else 'OBSERVATION_UNIT_SENSITIVITY_ONLY',
                       'period_selection_basis': 'COHORT_SELECTED_ON_FULL_REUSED_LEDGER_BEFORE_PERIOD_FILTER',
                       'denominator_definition': 'Selected QC-valid nonnegative recorded interactions; not all traffic exposure'}
                ranked.append(row)
            _ranks(ranked); output.extend(ranked)
    return output


def _multiplicity(frame: pd.DataFrame) -> pd.DataFrame:
    pair = ['scenario_id', 'encroaching_object_id', 'priority_object_id']
    signed = frame.loc[frame['_eligible']]
    grouped = signed.groupby(pair, sort=True, observed=True).size().rename('n_qc_valid_events')
    diagnostics = grouped.reset_index()
    have_coordinates = all(column in signed for column in ('conflict_lon_deg', 'conflict_lat_deg'))
    if have_coordinates:
        coordinates = signed.dropna(subset=['conflict_lon_deg', 'conflict_lat_deg']).drop_duplicates([*pair, 'conflict_lon_deg', 'conflict_lat_deg'])
        variants = coordinates.groupby(pair, observed=True).size().rename('n_provider_conflict_coordinate_variants')
        diagnostics = diagnostics.merge(variants.reset_index(), on=pair, how='left')
        diagnostics['n_provider_conflict_coordinate_variants'] = diagnostics['n_provider_conflict_coordinate_variants'].fillna(0).astype('int64')
    else:
        diagnostics['n_provider_conflict_coordinate_variants'] = pd.NA
    result = []
    for scenario, values in diagnostics.groupby('scenario_id', sort=True, observed=True):
        multiple = values['n_provider_conflict_coordinate_variants'].gt(1).fillna(False)
        result.append({'scenario_id': scenario, 'n_pairs': len(values), 'n_qc_valid_events': int(values['n_qc_valid_events'].sum()),
                       'n_pairs_with_repeated_events': int(values['n_qc_valid_events'].gt(1).sum()),
                       'n_events_in_repeated_pairs': int(values.loc[values['n_qc_valid_events'].gt(1), 'n_qc_valid_events'].sum()),
                       'n_pairs_multiple_provider_conflict_coordinates': int(multiple.sum()) if have_coordinates else None,
                       'n_events_in_multiple_coordinate_pairs': int(values.loc[multiple, 'n_qc_valid_events'].sum()) if have_coordinates else None,
                       'n_events_removed_by_first_representative': int(values['n_qc_valid_events'].sum() - len(values)),
                       'representative_suitability': 'EXPLORATORY_ONLY_SPATIAL_EQUIVALENCE_NOT_VERIFIED',
                       'limitation': 'Distinct reported coordinates may reflect multiple conflict points or numerical/geometry variation; not independently classified. A retained unchanged.'})
    return pd.DataFrame(result, columns=['scenario_id', 'n_pairs', 'n_qc_valid_events', 'n_pairs_with_repeated_events',
        'n_events_in_repeated_pairs', 'n_pairs_multiple_provider_conflict_coordinates', 'n_events_in_multiple_coordinate_pairs',
        'n_events_removed_by_first_representative', 'representative_suitability', 'limitation'])


def _bootstrap(frame: pd.DataFrame, scenarios: list[str], thresholds: tuple[float, ...]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    dates = sorted(frame.loc[frame['_eligible'], '_date'].dropna().unique().tolist())
    generator = np.random.default_rng(20261012)
    indices = generator.integers(0, len(dates), size=(200, len(dates))) if dates else np.empty((200, 0), dtype=np.int64)
    weights = np.zeros((200, len(dates)), dtype=np.int64)
    for replicate in range(200):
        if dates:
            weights[replicate] = np.bincount(indices[replicate], minlength=len(dates))
    digest = hashlib.sha256(json.dumps({'dates': dates, 'indices': indices.tolist()}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    draws = pd.DataFrame([{'replicate': replicate, 'draw_position': position, 'drawn_date_index': int(index), 'drawn_local_date': dates[index]}
                          for replicate, sample in enumerate(indices) for position, index in enumerate(sample)],
                         columns=['replicate', 'draw_position', 'drawn_date_index', 'drawn_local_date'])
    replicates = []; summaries = []
    columns = [*scenarios, 'ALL']
    date_index = pd.Index(dates, name='_date')
    for unit in UNITS:
        selected = frame.loc[frame['_' + unit]]
        nonnegative = selected.loc[selected['pet_s'].ge(0)]
        support = nonnegative.groupby('scenario_id', observed=True).agg(n=('event_key', 'size'), dates=('_date', 'nunique'))
        eligible = set(support.index[(support['n'] >= MIN_BOOTSTRAP_EVENTS) & (support['dates'] >= MIN_BOOTSTRAP_DATES)])
        def matrix(values: pd.DataFrame) -> np.ndarray:
            if not dates:
                return np.zeros((0, len(columns)), dtype=np.int64)
            counts = values.groupby(['_date', 'scenario_id'], observed=True).size().unstack(fill_value=0)
            counts = counts.reindex(index=date_index, columns=scenarios, fill_value=0).fillna(0).astype('int64')
            result = counts.to_numpy(); return np.column_stack([result, result.sum(axis=1)])
        denominator_counts = weights @ matrix(nonnegative)
        selected_counts = weights @ matrix(selected)
        negative_counts = weights @ matrix(selected.loc[selected['pet_s'].lt(0)])
        for tau in thresholds:
            numerator_counts = weights @ matrix(nonnegative.loc[nonnegative['pet_s'].le(tau)])
            records = []
            for replicate in range(200):
                ranked = []
                for j, scenario in enumerate(columns):
                    numerator = int(numerator_counts[replicate, j]); denominator = int(denominator_counts[replicate, j])
                    ranked.append({'unit': unit, 'scenario_id': scenario, 'tau_s': tau, 'replicate': replicate,
                                   'numerator': numerator, 'denominator': denominator, 'fraction': numerator / denominator if denominator else None,
                                   'n_selected_weighted': int(selected_counts[replicate, j]), 'n_negative_weighted': int(negative_counts[replicate, j]),
                                   'date_resample_sha256': digest, 'resample_scope': 'SHARED_OBSERVED_LOCAL_DATES_EXPLORATORY_NOT_INDEPENDENT_POPULATION'})
                _ranks(ranked, eligible); records.extend(ranked)
            replicates.extend(records)
            for scenario in columns:
                values = [row for row in records if row['scenario_id'] == scenario]
                ranks = [row['rank'] for row in values if row['rank'] is not None]
                summary = {'unit': unit, 'scenario_id': scenario, 'tau_s': tau, 'repetitions': 200, 'seed': 20261012,
                           'n_valid_fraction_replicates': sum(row['denominator'] > 0 for row in values),
                           'n_valid_rank_replicates': len(ranks), 'n_zero_denominator_replicates': sum(row['denominator'] == 0 for row in values),
                           'n_tied_rank_replicates': sum(row['tie_count'] is not None and row['tie_count'] > 1 for row in values),
                           'rank_min': min(ranks) if ranks else None, 'rank_max': max(ranks) if ranks else None,
                           'rank_quantile_025': float(np.quantile(ranks, .025)) if ranks else None,
                           'rank_quantile_975': float(np.quantile(ranks, .975)) if ranks else None,
                           'rank_distribution': json.dumps(dict(sorted((str(rank), ranks.count(rank)) for rank in set(ranks))), sort_keys=True),
                           'n_missing_date_nonnegative_events_excluded': int(nonnegative['_date'].isna().sum()) if scenario == 'ALL' else int(nonnegative.loc[nonnegative['scenario_id'].eq(scenario), '_date'].isna().sum()),
                           'bootstrap_rank_eligible': scenario in eligible, 'minimum_events': MIN_BOOTSTRAP_EVENTS, 'minimum_observed_dates': MIN_BOOTSTRAP_DATES,
                           'rank_reason': 'ALL_AGGREGATE_NOT_RANKED' if scenario == 'ALL' else 'COMMON_DATE_EXPLORATORY_RANK_SENSITIVITY' if scenario in eligible else 'INSUFFICIENT_BASELINE_BOOTSTRAP_SUPPORT',
                           'interval_interpretation': 'Empirical bootstrap rank range/quantiles; not a population/generalization confidence interval',
                           'date_resample_sha256': digest}
                summaries.append(summary)
    return pd.DataFrame(replicates), pd.DataFrame(summaries), draws, {'status': 'COMPUTED_EXPLORATORY' if dates else 'NOT_APPLICABLE_NO_LOCAL_DATES',
        'repetitions': 200, 'seed': 20261012, 'date_pool': dates, 'date_pool_definition': 'Union of observed local dates among all QC-valid signed events',
        'shared_date_resample_sha256': digest, 'same_indices_for_every_unit_scenario_and_threshold': True,
        'minimum_events': MIN_BOOTSTRAP_EVENTS, 'minimum_observed_dates': MIN_BOOTSTRAP_DATES,
        'independent_dates_assumed': False}


def analyze_units(ledger_df: pd.DataFrame, thresholds=(.5, 1, 1.5, 2), bootstrap_repetitions=200, seed=20261012,
                  include_selection=True) -> dict[str, Any]:
    """Return stable pandas tables and JSON-ready metadata; writes no files.

    Input must be the reconciled, unique-key frozen event ledger, not raw provider
    rows. n_raw is source rows represented by that ledger, before QC/selection.
    n_pairs is scenario+ordered-pair uniqueness within the selected cohort.
    C n_selected includes negative first representatives; its denominator does
    not. Monthly/leave-one-month counts never reselect a representative.
    """
    if bootstrap_repetitions != 200 or seed != 20261012:
        raise ValueError('R2_BOOTSTRAP_FIXED_B200_SEED20261012')
    thresholds = tuple(sorted(float(value) for value in thresholds))
    if not thresholds or len(set(thresholds)) != len(thresholds) or any(not math.isfinite(value) or value < 0 for value in thresholds):
        raise ValueError('R2_THRESHOLDS_INVALID')
    frame, first = prepare_ledger(ledger_df)
    scenarios = sorted(frame['scenario_id'].unique().tolist())
    all_mask = pd.Series(True, index=frame.index)
    points = _point_rows(frame, scenarios, thresholds, 'FULL_PERIOD', 'ALL', all_mask)
    monthly = []; leave_one = []; period_support = []
    months = sorted(frame.loc[frame['_eligible'], '_month'].dropna().unique().tolist())
    for month in months:
        in_month = frame['_month'].eq(month).fillna(False)
        monthly.extend(_point_rows(frame, scenarios, thresholds, 'MONTH', month, in_month))
        leave_one.extend(_point_rows(frame, scenarios, thresholds, 'LEAVE_ONE_MONTH_OUT', month, ~in_month))
        period_support.append({'local_month': month, 'n_raw': int(frame.loc[in_month, 'n_source_rows'].sum()),
                               'n_unique_events': int(in_month.sum()), 'n_qc_valid_signed': int((in_month & frame['_eligible']).sum()),
                               'n_observed_local_dates': int(frame.loc[in_month & frame['_eligible'], '_date'].nunique()),
                               'scope': 'Observed calendar dates, not asserted independent observation days'})
    reference_ranks = {(row['unit'], row['scenario_id'], row['tau_s']): row['rank'] for row in points}
    for row in [*monthly, *leave_one]:
        baseline_rank = reference_ranks[row['unit'], row['scenario_id'], row['tau_s']]
        row['full_period_rank'] = baseline_rank
        row['rank_change_from_full_period'] = row['rank'] - baseline_rank if row['rank'] is not None and baseline_rank is not None else None
    for row in points:
        a_rank = reference_ranks['A', row['scenario_id'], row['tau_s']]
        row['original_A_rank'] = a_rank
        row['rank_change_from_A'] = row['rank'] - a_rank if row['rank'] is not None and a_rank is not None else None
    replicates, boot_summary, draws, boot_metadata = _bootstrap(frame, scenarios, thresholds)
    period_columns = [key for key in points[0] if key not in ('original_A_rank', 'rank_change_from_A')] + ['full_period_rank', 'rank_change_from_full_period'] if points else []
    tables = {'unit_summary': pd.DataFrame(points), 'monthly_rank_sensitivity': pd.DataFrame(monthly, columns=period_columns),
              'leave_one_month_rank_sensitivity': pd.DataFrame(leave_one, columns=period_columns), 'bootstrap_rank_replicates': replicates,
              'bootstrap_rank_summary': boot_summary, 'bootstrap_date_resamples': draws,
              'period_support': pd.DataFrame(period_support, columns=['local_month', 'n_raw', 'n_unique_events', 'n_qc_valid_signed', 'n_observed_local_dates', 'scope']), 'pair_multiplicity': _multiplicity(frame)}
    if include_selection:
        names = ['event_key', 'event_id', 'scenario_id', 'encroaching_object_id', 'priority_object_id', 'pet_s', 'qc_status', 'n_source_rows',
                 '_date', '_representative_time_ms', '_A', '_B', '_C', '_D']
        selected = frame[names].rename(columns={'_date': 'event_date_local', '_representative_time_ms': 'representative_order_timestamp_ms',
                    '_A': 'selected_A', '_B': 'selected_B', '_C': 'selected_C', '_D': 'selected_D'})
        tables['unit_selection'] = selected.sort_values('event_key', kind='stable').reset_index(drop=True)
    metadata = {'profile_id': 'PET_OBSERVATION_UNITS_R2', 'additional_actual_fit_calls': 0,
                'input_definition': 'Reconciled immutable PET ledger; original event baseline retained',
                'units': UNIT_DEFINITIONS, 'representative_order': list(REPRESENTATIVE_ORDER),
                'representative_timestamp_basis': 'Minimum of the four original event UTC millisecond timestamps; signal clock unused',
                'negative_representatives_replaced_by_positive': False, 'D_filter_applied_after_C_selection': True,
                'period_selection_basis': 'Select A/B/C/D once over the full reused ledger, then slice periods; never reselect representatives',
                'n_raw_definition': 'Sum n_source_rows for all input ledger rows in the period, before QC or unit selection',
                'n_pairs_definition': 'Unique scenario + encroaching_object_id + priority_object_id among selected unit rows; ordered roles never swapped',
                'n_selected_definition': 'Selected rows before negative representatives are removed from the conditional denominator',
                'denominator_definition': 'Selected QC-valid nonnegative provider interactions',
                'thresholds_s': list(thresholds), 'bootstrap': boot_metadata, 'n_input_unique_events': len(frame),
                'n_input_source_rows': int(frame['n_source_rows'].sum()), 'n_qc_valid_signed': int(frame['_eligible'].sum()),
                'n_first_pair_representatives': len(first), 'n_qc_valid_missing_local_date': int((frame['_eligible'] & frame['_date'].isna()).sum()),
                'n_input_missing_local_date': int(frame['_date'].isna().sum()),
                'manual_review_status': 'NOT_PERFORMED', 'independent_accuracy': None,
                'limits': ['C/D are sensitivity units: spatial equivalence of repeated pair records is not independently verified.',
                           'Coordinate multiplicity is a numerical diagnostic, not an independent geometric-zone classification.',
                           'Fractions/ranks describe recorded interactions, not vehicle exposure, accident probability or policy priority.',
                           'Same observed date draws are shared across all scenarios; date independence and population generalization are not assumed.']}
    return {'tables': tables, 'metadata': metadata}
