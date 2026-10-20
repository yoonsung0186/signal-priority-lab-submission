"""No-fit quality hold supplement over immutable R2 event/condition tables.

The provider intervals and literal signal states do not change. A quality hold
is separate from signal-state uncertainty or a new clock-sensitivity result.
Only newly held nonnegative events need zero-offset literal state lookups.
"""
from __future__ import annotations

from collections import Counter
from numbers import Integral
import re

import numpy as np
import pandas as pd

from .r2_signals import CONDITION_STATES, OFFSETS, POPULATIONS, ROLES, offset_milliseconds

QUALITY = 'SOURCE_QUALITY_UNRESOLVED_LONG_INTERVAL'
MODE = 'QUALITY_HOLD_ONLY_NOT_NEW_SIGNAL_STATE_SENSITIVITY'
INT_MIN, INT_MAX = -(2**63), 2**63 - 1
REQUIRED_LEDGER = ('event_key', 'scenario_id', 'pet_s', 'qc_status',
                   'ts_enter_encroaching_ms', 'ts_enter_priority_ms')
REQUIRED_CROSSINGS = ('signal_group_id', 'start_timestamp_ms', 'end_timestamp_ms',
                     'candidate_period_start_ms', 'candidate_period_end_ms')
REQUIRED_CONDITIONS = ('scenario_id', 'encroaching_entry_signal', 'priority_entry_signal',
                      'tau_s', 'n_short_pet', 'n_valid_nonnegative_pet',
                      'candidate_period_n_valid_nonnegative_pet', 'candidate_period_n_short_pet')


def _integer(value, *, nullable=False):
    """No float/scientific/rounded timestamp coercion, even for integral floats."""
    if value is None or value is pd.NA or (not isinstance(value, (str, Integral)) and pd.isna(value)) or value == '':
        if nullable:
            return None
        raise ValueError('Missing required integer')
    if isinstance(value, (bool, np.bool_)):
        raise ValueError('Boolean is not an integer timestamp/count')
    if isinstance(value, Integral):
        number = int(value)
    elif isinstance(value, str) and re.fullmatch(r'[+-]?[0-9]+', value):
        number = int(value)
    else:
        raise ValueError('Exact integer or decimal integer string required')
    if not INT_MIN <= number <= INT_MAX:
        raise ValueError('Integer outside signed 64-bit range')
    return number


def _times(series):
    values = np.zeros(len(series), dtype=np.int64)
    valid = np.zeros(len(series), dtype=bool)
    for i, raw in enumerate(series.array):
        value = _integer(raw, nullable=True)
        if value is not None:
            values[i] = value
            valid[i] = True
    return values, valid


def _present_strings(series, label):
    values = series.to_numpy(copy=True)
    if any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError(label + ' must contain nonempty strings')
    return values


def _group(mappings, scenario, role):
    entry = mappings.get(scenario)
    if not entry or entry.get('conflict'):
        return None
    raw = entry.get('raw', entry)
    value = raw.get(role + '_signal_group_id')
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Signal group mapping must be a nonempty string')
    return value


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _intervals(crossings):
    if not isinstance(crossings, pd.DataFrame) or not set(REQUIRED_CROSSINGS) <= set(crossings.columns) or crossings.empty:
        raise ValueError('Nonempty crossing interval table with candidate-period bounds required')
    table = crossings.copy(deep=True)
    groups = _present_strings(table['signal_group_id'], 'Signal groups')
    for field in REQUIRED_CROSSINGS[1:]:
        table[field] = [_integer(v) for v in table[field].array]
    periods = table[['candidate_period_start_ms', 'candidate_period_end_ms']].drop_duplicates()
    if len(periods) != 1:
        raise ValueError('Crossings must bind one original candidate period')
    lo, hi = map(int, periods.iloc[0])
    if lo >= hi:
        raise ValueError('Invalid original candidate period')
    by_group = {}
    for group, start, end in zip(groups, table['start_timestamp_ms'], table['end_timestamp_ms']):
        start, end = int(start), int(end)
        if start >= end or start >= hi or end <= lo or not (start < lo or end > hi):
            raise ValueError('Crossing must be a nonempty half-open interval extending beyond the original period')
        by_group.setdefault(group, []).append((start, end))
    table['source_quality_status'] = QUALITY
    table['quality_interval_rule'] = 'ORIGINAL_INTEGER_[start,end)_NO_EXTENSION_OR_FFILL'
    table['quality_hold_mode'] = MODE
    return table, by_group, lo, hi


def _validated_conditions(literal, scenarios, pet, eligible, original_hold):
    if not isinstance(literal, pd.DataFrame) or not set(REQUIRED_CONDITIONS) <= set(literal.columns) or literal.empty:
        raise ValueError('Literal condition table schema required')
    result = literal.copy(deep=True)
    sid = _present_strings(result['scenario_id'], 'Condition scenarios')
    for field in ('encroaching_entry_signal', 'priority_entry_signal'):
        if not result[field].isin(CONDITION_STATES).all():
            raise ValueError('Unrecognized literal condition state')
    tau = pd.to_numeric(result['tau_s'], errors='raise').to_numpy(dtype=float)
    if not np.isfinite(tau).all() or (tau < 0).any():
        raise ValueError('Finite nonnegative condition thresholds required')
    if 'clock_offset_s' in result:
        offsets = pd.to_numeric(result['clock_offset_s'], errors='raise').to_numpy(dtype=float)
        if not np.isfinite(offsets).all() or (offsets != 0).any():
            raise ValueError('Literal condition table must be offset zero')
    keys = [(s, e, p, float(t)) for s, e, p, t in zip(sid, result['encroaching_entry_signal'], result['priority_entry_signal'], tau)]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate literal condition cell')
    counts = {}
    fields = ('n_valid_nonnegative_pet', 'n_short_pet', 'candidate_period_n_valid_nonnegative_pet', 'candidate_period_n_short_pet')
    for field in fields:
        counts[field] = np.asarray([_integer(v) for v in result[field].array], dtype=np.int64)
        if (counts[field] < 0).any():
            raise ValueError('Negative literal condition count')
    n, short, old, oldshort = (counts[f] for f in fields)
    if ((short > n) | (old > n) | (oldshort > old) | (oldshort > short) | ((short - oldshort) > (n - old))).any():
        raise ValueError('Literal/old-hold count bounds inconsistent')
    represented = set(sid)
    if not set(scenarios[eligible]) <= represented:
        raise ValueError('Eligible ledger scenario missing from literal table')
    thresholds = sorted(set(tau))
    for scenario in sorted(represented):
        row_mask = sid == scenario
        if set(tau[row_mask]) != set(thresholds):
            raise ValueError('Scenario threshold sets differ')
        event_mask = eligible if scenario == 'ALL_SCENARIOS' else eligible & (scenarios == scenario)
        for threshold in thresholds:
            cells = row_mask & (tau == threshold)
            expected = (int(event_mask.sum()), int((event_mask & (pet <= threshold)).sum()),
                        int((event_mask & original_hold).sum()), int((event_mask & original_hold & (pet <= threshold)).sum()))
            actual = tuple(int(counts[field][cells].sum()) for field in fields)
            if actual != expected:
                raise ValueError('Literal/old-date scenario threshold sums disagree with preserved ledger')
    # Denominators and original-date hold are invariant across thresholds.
    fixed = {}
    for i, (scenario, enc, priority, _) in enumerate(keys):
        key = (scenario, enc, priority)
        value = (int(n[i]), int(old[i]))
        if key in fixed and fixed[key] != value:
            raise ValueError('Literal cell denominator changes across thresholds')
        fixed[key] = value
    return result, keys, thresholds, counts


def analyze_boundary(ledger, crossings, mappings, literal_conditions, lookup, *, offsets_s=OFFSETS):
    """Return DataFrames and summary without writes, PET/unit/rank/AI recomputation.

    QC-valid signed rows require both own-entry clocks; null clocks are rejected
    instead of trusting a potentially known literal cell. Invalid rows' null
    clocks never receive membership or a guessed state. Non-null timestamps
    must be exact signed-64-bit integers or integer strings. Original date hold
    uses either ORIGINAL own-role entry in the crossing table's candidate period
    and is fixed across offsets. Group-specific long hold uses each shifted own
    entry in the ORIGINAL provider [start,end). Overlapping listed intervals
    union membership; neither role queries nor unique events are multiplied.
    """
    if not isinstance(ledger, pd.DataFrame) or not set(REQUIRED_LEDGER) <= set(ledger.columns):
        raise ValueError('Boundary ledger schema required')
    if not isinstance(mappings, dict) or not callable(lookup):
        raise ValueError('Mappings dictionary and literal lookup callable required')
    for scenario, entry in mappings.items():
        if not isinstance(scenario, str) or not scenario.strip() or scenario == 'ALL_SCENARIOS' or not isinstance(entry, dict):
            raise ValueError('Invalid scenario signal mapping')
        if 'raw' in entry and not isinstance(entry['raw'], dict):
            raise ValueError('Invalid raw scenario signal mapping')
    keys = _present_strings(ledger['event_key'], 'Event keys')
    if len(keys) != len(set(keys)):
        raise ValueError('Preserved ledger event keys must be unique')
    scenarios = _present_strings(ledger['scenario_id'], 'Scenarios')
    if 'ALL_SCENARIOS' in set(scenarios):
        raise ValueError('ALL_SCENARIOS is reserved for aggregation')
    status = ledger['qc_status'].to_numpy(copy=True)
    if not ledger['qc_status'].isin(('VALID_NONNEGATIVE_PET', 'NEGATIVE_PET_REVIEW', 'INVALID')).all():
        raise ValueError('Unrecognized preserved QC status')
    pet = pd.to_numeric(ledger['pet_s'], errors='coerce').to_numpy(dtype=float)
    signed = np.isin(status, ('VALID_NONNEGATIVE_PET', 'NEGATIVE_PET_REVIEW'))
    eligible = status == 'VALID_NONNEGATIVE_PET'
    if not np.isfinite(pet[signed]).all() or (pet[eligible] < 0).any() or (pet[status == 'NEGATIVE_PET_REVIEW'] >= 0).any():
        raise ValueError('Preserved signed PET/QC contradiction')
    offsets = [(float(value), offset_milliseconds(value)) for value in offsets_s]
    if not offsets or len({ms for _, ms in offsets}) != len(offsets) or 0 not in {ms for _, ms in offsets}:
        raise ValueError('Distinct nonempty offsets including zero required')
    quality, intervals, lo, hi = _intervals(crossings)
    times, valid, groups, group_indices = {}, {}, {}, {}
    scenario_names = sorted(set(scenarios) | set(mappings))
    for role in ROLES:
        times[role], valid[role] = _times(ledger['ts_enter_' + role + '_ms'])
        if (signed & ~valid[role]).any():
            raise ValueError('QC-valid signed null own-entry timestamp is not accepted')
        if valid[role].any():
            low, high = int(times[role][valid[role]].min()), int(times[role][valid[role]].max())
            if any(low + ms < INT_MIN or high + ms > INT_MAX for _, ms in offsets):
                raise ValueError('Shifted event timestamp exceeds signed 64-bit range')
        role_map = {s: _group(mappings, s, role) for s in set(scenarios)}
        groups[role] = np.asarray([role_map[s] or ('UNMAPPED:' + s + ':' + role) for s in scenarios], dtype=object)
        group_indices[role] = {g: np.flatnonzero(groups[role] == g) for g in sorted(set(groups[role]) | set(intervals))}
    original_role = {r: valid[r] & (times[r] >= lo) & (times[r] < hi) for r in ROLES}
    original_hold = original_role[ROLES[0]] | original_role[ROLES[1]]
    literal, cell_keys, thresholds, counts = _validated_conditions(literal_conditions, scenarios, pet, eligible, original_hold)
    populations = {POPULATIONS[0]: np.ones(len(ledger), dtype=bool), POPULATIONS[1]: eligible,
                   POPULATIONS[2]: eligible & (pet <= 1)}
    scenario_masks = {s: scenarios == s for s in scenario_names}
    scenario_masks['ALL_SCENARIOS'] = np.ones(len(ledger), dtype=bool)
    impact_rows, role_rows = [], []
    zero_long = None
    for offset, delta in offsets:
        left, right, long = {}, {}, {}
        for role in ROLES:
            left[role] = np.zeros(len(ledger), dtype=bool)
            right[role] = np.zeros(len(ledger), dtype=bool)
            long[role] = np.zeros(len(ledger), dtype=bool)
            shifted = times[role] + delta
            for group, segments in intervals.items():
                ix = group_indices[role].get(group)
                if ix is None:
                    continue
                values, usable = shifted[ix], valid[role][ix]
                for start, end in segments:
                    inside = usable & (values >= start) & (values < end)
                    long[role][ix] |= inside
                    left[role][ix] |= inside & (values < lo)
                    right[role][ix] |= inside & (values >= hi)
        left_event = left[ROLES[0]] | left[ROLES[1]]
        right_event = right[ROLES[0]] | right[ROLES[1]]
        tails = left_event | right_event
        long_event = long[ROLES[0]] | long[ROLES[1]]
        union = original_hold | long_event
        if delta == 0:
            zero_long = long_event.copy()
        for population, pop_mask in populations.items():
            for scenario, scenario_mask in scenario_masks.items():
                mask = pop_mask & scenario_mask
                n = int(mask.sum())
                row = {'clock_offset_s': offset, 'population': population, 'scenario_id': scenario,
                       'fixed_population_events': n, 'left_tail_role_queries': sum(int((mask & left[r]).sum()) for r in ROLES),
                       'right_tail_role_queries': sum(int((mask & right[r]).sum()) for r in ROLES),
                       'left_tail_unique_events': int((mask & left_event).sum()), 'right_tail_unique_events': int((mask & right_event).sum()),
                       'tail_unique_events': int((mask & tails).sum()), 'original_date_hold_unique_events': int((mask & original_hold).sum()),
                       'original_date_hold_own_entry_role_queries': sum(int((mask & original_role[r]).sum()) for r in ROLES),
                       'long_interval_held_unique_events': int((mask & long_event).sum()),
                       'long_interval_held_role_queries': sum(int((mask & long[r]).sum()) for r in ROLES),
                       'new_tail_unique_events_beyond_original_date_hold': int((mask & tails & ~original_hold).sum()),
                       'new_long_interval_unique_events_beyond_original_date_hold': int((mask & long_event & ~original_hold).sum()),
                       'union_held_unique_events': int((mask & union).sum()), 'quality_hold_mode': MODE}
                row['tail_role_queries'] = row['left_tail_role_queries'] + row['right_tail_role_queries']
                row['union_held_event_fraction'] = _ratio(row['union_held_unique_events'], n)
                impact_rows.append(row)
            for role in ROLES:
                for group, ix in group_indices[role].items():
                    mask = pop_mask[ix]
                    row = {'clock_offset_s': offset, 'population': population, 'signal_group_id': group, 'role': role,
                           'fixed_population_role_queries': int(mask.sum()), 'quality_hold_mode': MODE}
                    for field, membership in (('left_tail', left[role]), ('right_tail', right[role]),
                                              ('long_interval_held', long[role]), ('original_date_hold_own_entry', original_role[role]),
                                              ('union_own_entry_held', original_role[role] | long[role])):
                        count = int((mask & membership[ix]).sum())
                        row[field + '_role_queries'] = count
                        row[field + '_unique_events'] = count
                    row['tail_role_queries'] = row['left_tail_role_queries'] + row['right_tail_role_queries']
                    row['tail_unique_events'] = row['tail_role_queries']
                    role_rows.append(row)
    newly_held = eligible & zero_long & ~original_hold
    additions = Counter()
    lookup_calls = 0
    represented = set(literal['scenario_id'])
    for i in np.flatnonzero(newly_held):
        states = []
        for role in ROLES:
            if not valid[role][i] or groups[role][i].startswith('UNMAPPED:'):
                state = 'UNKNOWN'
            else:
                answer = lookup(groups[role][i], int(times[role][i]))
                lookup_calls += 1
                if not isinstance(answer, dict) or answer.get('state') not in CONDITION_STATES:
                    raise ValueError('Literal lookup returned an unrecognized state')
                state = answer['state']
            states.append(state)
        for scenario in (scenarios[i], 'ALL_SCENARIOS'):
            if scenario not in represented:
                continue
            for threshold in thresholds:
                cell = (scenario, *states, threshold)
                additions[(cell, 'n')] += 1
                additions[(cell, 'short')] += bool(pet[i] <= threshold)
    if {cell for cell, _ in additions} - set(cell_keys):
        raise ValueError('Newly held literal state pair missing from condition table')
    extra = []
    for i, cell in enumerate(cell_keys):
        new_n, new_short = additions[(cell, 'n')], additions[(cell, 'short')]
        quality_n = int(counts['candidate_period_n_valid_nonnegative_pet'][i]) + new_n
        quality_short = int(counts['candidate_period_n_short_pet'][i]) + new_short
        n, short = int(counts['n_valid_nonnegative_pet'][i]), int(counts['n_short_pet'][i])
        if quality_n > n or quality_short > short or quality_short > quality_n or short - quality_short > n - quality_n:
            raise ValueError('Quality partition exceeds literal cell counts')
        unknown = any(state in ('UNKNOWN', 'SIGNAL_AMBIGUOUS') for state in cell[1:3])
        remaining_n, remaining_short = n - quality_n, short - quality_short
        extra.append({'new_long_interval_held_n_valid_nonnegative_pet': new_n, 'new_long_interval_held_n_short_pet': new_short,
                      'quality_held_n_valid_nonnegative_pet': quality_n, 'quality_held_n_short_pet': quality_short,
                      'quality_held_short_pet_fraction': _ratio(quality_short, quality_n),
                      'remaining_unknown_n_valid_nonnegative_pet': remaining_n if unknown else 0,
                      'remaining_unknown_n_short_pet': remaining_short if unknown else 0,
                      'interpretable_n_valid_nonnegative_pet': 0 if unknown else remaining_n,
                      'interpretable_n_short_pet': 0 if unknown else remaining_short,
                      'interpretable_short_pet_fraction': None if unknown else _ratio(remaining_short, remaining_n),
                      'quality_partition_status': 'LITERAL_EQUALS_QUALITY_PLUS_REMAINING_UNKNOWN_PLUS_INTERPRETABLE',
                      'quality_hold_mode': MODE})
    extra_frame = pd.DataFrame(extra, index=literal.index)
    if set(extra_frame.columns) & set(literal.columns):
        raise ValueError('Supplement columns already exist; literal columns must not be overwritten')
    condition_quality = pd.concat((literal, extra_frame), axis=1)
    summary = {'status': 'COMPLETED_BOUNDARY_QUALITY_SUPPLEMENT', 'quality_hold_mode': MODE,
               'n_ledger_events': len(ledger), 'n_quality_interval_rows': len(quality), 'n_quality_signal_groups': len(intervals),
               'candidate_period_start_ms': lo, 'candidate_period_end_ms': hi,
               'clock_offsets_s': [offset for offset, _ in offsets], 'n_newly_held_nonnegative_events_offset_zero': int(newly_held.sum()),
               'literal_lookup_calls': lookup_calls, 'lookup_scope': 'ONLY_NEW_LONG_HELD_NONNEGATIVE_EVENTS_OFFSET_ZERO',
               'original_date_hold_basis': 'ANY_ORIGINAL_OWN_ROLE_ENTRY_[candidate_start,candidate_end)_FIXED_ACROSS_OFFSETS',
               'long_hold_basis': 'ANY_SHIFTED_OWN_ROLE_ENTRY_IN_ITS_GROUP_ORIGINAL_[start,end)',
               'condition_partition_checks': 'LITERAL_AND_ORIGINAL_DATE_HOLD_SCENARIO_THRESHOLD_SUMS_MATCH_LEDGER',
               'nullable_timestamp_policy': 'QC_VALID_SIGNED_NULL_OWN_ENTRY_REJECTED; INVALID_ROW_NULL_CLOCK_NO_MEMBERSHIP_OR_GUESSED_STATE',
               'n_null_role_timestamps': sum(int((~valid[r]).sum()) for r in ROLES),
               'signal_clock_shifted': False, 'raw_signal_states_changed': False, 'pet_and_input_dataframes_mutated': False,
               'baseline_recomputed': False, 'additional_fit_calls': 0,
               'limitations': ['Source quality remains unresolved; a file interval is not proof of physically observed continuous signals.',
                               'Old held cell counts are preserved and aggregate-checked, not independently relabeled.',
                               'Unknown/ambiguous remaining cells are not interpreted as safe or zero risk.']}
    return {'tables': {'quality_intervals': quality, 'boundary_impact': pd.DataFrame(impact_rows),
                       'boundary_role_impact': pd.DataFrame(role_rows), 'signal_condition_quality': condition_quality},
            'summary': summary}
