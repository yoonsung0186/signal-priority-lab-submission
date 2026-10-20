"""Source-backed numerical review of a frozen PET run; never fits a model.

The audit is automated consistency evidence. It is neither human annotation nor
an independent reconstruction of conflict geometry. Source rows are streamed;
only the predeclared audit cohort and bounded saved AI cohort are retained.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import sys

from .contracts import ContractError
from .pet_contracts import TIMES, finite, integer, key_value, stable_json
from .pet_io import file_hash, load_tables
from .pet_qc import build_ledger, event_time
from .pet_signals import SignalIndex, STATES
from .pet_artifacts import write_json, write_table
from .safety import child, new_run, verify_root

ROLES = ('encroaching', 'priority')
SELECTION_RULE = 'ALL_FINITE_NEGATIVE_PET_ALL_QC_VALID_0_TO_1S_PLUS_NUMERIC_EVENT_ID_FIRST_3_PER_SCENARIO'
FORMULAE = {
    'explicit_count': 'Count rows or reconciled keys in the explicitly declared population',
    'explicit_count_over_declared_population': 'numerator / explicitly declared denominator; denominator zero gives null',
    'provider_timestamp_identity': '(ts_enter_priority_ms - ts_leave_encroaching_ms) / 1000',
    'conditional_short_fraction': 'n(QC-valid and 0 <= provider PET <= threshold) / n(QC-valid and provider PET >= 0)',
    'centroid_feature_share': 'sum_over_unordered_centroid_pairs((center_a_j-center_b_j)^2) / sum_over_features_and_pairs',
    'frozen_subset_agreement': 'n(argmin frozen squared distance on selected dimensions == saved cluster) / n(saved assignments)',
    'descriptive_majority_agreement': 'sum_group(max_cluster_count_in_group) / n(saved assignments)',
    'iqr_tail_flag': 'observed feature < Q1-1.5*(Q3-Q1) or > Q3+1.5*(Q3-Q1); not a measurement-error label',
}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def decode_row(row):
    result = {}
    for key, value in row.items():
        if value == '':
            result[key] = None
        elif value in ('True', 'False'):
            result[key] = value == 'True'
        elif value and value[0] in '[{':
            try:
                result[key] = json.loads(value)
            except ValueError:
                result[key] = value
        elif key in TIMES or key in ('cluster', 'n_source_rows'):
            result[key] = integer(value)
        elif key == 'pet_s' or key.endswith(('_occupancy_s', '_length_m', '_width_m', '_lon_deg', '_lat_deg')):
            result[key] = finite(value)
        else:
            result[key] = value
    return result


def csv_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            yield decode_row(row)


def select_cases(rows, ordinary_per_scenario=3):
    """Fixed before reading results; ordinary selection ignores PET and validity."""
    chosen = {}
    ordinary = defaultdict(list)
    counts = Counter()
    scenario_counts = defaultdict(Counter)
    dates = set()
    local_dates = set()
    minimum = maximum = None
    for row in rows:
        counts['unique_ledger_rows'] += 1
        counts['source_rows_represented'] += int(row.get('n_source_rows') or 1)
        status = row.get('qc_status')
        counts[status or 'MISSING_QC_STATUS'] += 1
        scenario = str(row.get('scenario_id') or 'UNKNOWN_SCENARIO')
        scenario_counts[scenario][status or 'MISSING_QC_STATUS'] += 1
        value = finite(row.get('pet_s'))
        if row.get('valid_nonnegative_pet') and value is not None:
            for tau in (.5, 1., 1.5, 2.):
                scenario_counts[scenario]['short_' + str(tau)] += 0 <= value <= tau
        counts['finite_signed_pet'] += value is not None
        counts['all_finite_negative_pet'] += value is not None and value < 0
        if row.get('event_date_utc'):
            dates.add(row['event_date_utc'])
        if row.get('event_date_local'):
            local_dates.add(row['event_date_local'])
        for field in TIMES:
            timestamp = integer(row.get(field))
            if timestamp is not None:
                minimum = timestamp if minimum is None else min(minimum, timestamp)
                maximum = timestamp if maximum is None else max(maximum, timestamp)
        reasons = []
        if value is not None and value < 0:
            reasons.append('ALL_FINITE_NEGATIVE_PET')
        if row.get('valid_nonnegative_pet') and value is not None and 0 <= value <= 1:
            reasons.append('ALL_QC_VALID_0_TO_1S')
            counts['valid_short_1s'] += 1
        if reasons:
            chosen[row['event_key']] = (row, set(reasons))
        identifier = integer(row.get('event_id'))
        if identifier is not None and identifier >= 0:
            cohort = ordinary[str(row.get('scenario_id'))]
            cohort.append((identifier, row['event_key'], row))
            cohort.sort(key=lambda item: (item[0], item[1]))
            del cohort[ordinary_per_scenario:]
    for cohort in ordinary.values():
        for _, key, row in cohort:
            if key not in chosen:
                chosen[key] = (row, set())
            chosen[key][1].add('PREDECLARED_NUMERIC_ID_FIRST_3_PER_SCENARIO')
    selected = [dict(row, selection_reasons=sorted(reasons)) for _, (row, reasons) in sorted(chosen.items())]
    totals = dict(sorted(counts.items()))
    totals['scenario_threshold_counts'] = [
        {'scenario_id': scenario, 'tau_s': tau, 'n_short_pet': values['short_' + str(tau)],
         'n_valid_nonnegative_pet': values['VALID_NONNEGATIVE_PET'],
         'short_pet_fraction': ratio(values['short_' + str(tau)], values['VALID_NONNEGATIVE_PET'])}
        for scenario, values in sorted(scenario_counts.items()) for tau in (.5, 1., 1.5, 2.)]
    return selected, totals, {
        'earliest_event_timestamp_ms': minimum, 'latest_event_timestamp_ms': maximum,
        'utc_calendar_dates': sorted(dates), 'local_calendar_dates': sorted(local_dates),
        'utc_calendar_date_count': len(dates), 'local_calendar_date_count': len(local_dates),
    }


def denominator_rows(counts, raw_counts):
    valid = counts.get('VALID_NONNEGATIVE_PET', 0)
    negative = counts.get('NEGATIVE_PET_REVIEW', 0)
    unique = counts.get('unique_ledger_rows', 0)
    return [
        {'metric_id': name, 'numerator': value, 'denominator': denominator,
         'fraction': ratio(value, denominator), 'population': population, 'unit': 'recorded interaction fraction'}
        for name, value, denominator, population in [
            ('raw_source_finite_negative', raw_counts.get('finite_negative', 0), raw_counts.get('n_raw', 0), 'All original provider rows, before QC or key reconciliation'),
            ('qc_valid_nonnegative', valid, unique, 'Unique reconciled event keys including explicit invalid rows'),
            ('qc_valid_negative_review', negative, unique, 'Unique reconciled event keys including explicit invalid rows'),
            ('qc_invalid', counts.get('INVALID', 0), unique, 'Unique reconciled event keys including explicit invalid rows'),
            ('valid_nonnegative_pet_le_1s', counts.get('valid_short_1s', 0), valid, 'QC-valid nonnegative provider interactions'),
        ]
    ]


def clock_review(rows):
    """Summarize the declared event-only offsets; no measured clock variance."""
    rows = list(rows)
    base = {row['scenario_id']: row for row in rows if finite(row.get('clock_offset_s')) == 0}
    output = []
    for row in rows:
        offset = finite(row.get('clock_offset_s'))
        unchanged = row.get('pet_values_unchanged') is True
        shifted = row.get('signal_clock_shifted') is True
        gap = finite(row.get('max_abs_relative_timestamp_gap_change_s'))
        if not unchanged or shifted or (gap is not None and gap > 1e-9):
            raise ContractError('FINISH_CLOCK_OFFSET_INVARIANCE_FAILED')
        reference = base.get(row['scenario_id'])
        if reference is None:
            raise ContractError('FINISH_CLOCK_OFFSET_ZERO_REFERENCE_MISSING')
        states = row.get('role_state_pair_counts') or {}
        base_states = reference.get('role_state_pair_counts') or {}
        denominator = integer(row.get('n_valid_nonnegative_pet')) or 0
        if denominator != (integer(reference.get('n_valid_nonnegative_pet')) or 0):
            raise ContractError('FINISH_CLOCK_COHORT_CHANGED')
        moved = sum(abs(int(states.get(pair, 0)) - int(base_states.get(pair, 0))) for pair in set(states) | set(base_states))
        output.append({'scenario_id': row['scenario_id'], 'clock_offset_s': offset,
                       'n_valid_nonnegative_pet': denominator, 'n_signal_known': integer(row.get('n_signal_known')),
                       'n_signal_ambiguous_or_unknown': integer(row.get('n_signal_ambiguous_or_unknown')),
                       'role_pair_distribution_total_variation_from_zero': moved / (2 * denominator) if denominator else None,
                       'pet_gap_change_s': gap, 'signal_clock_shifted': False,
                       'measured_clock_error_variance': None, 'basis': 'ASSUMED_EVENT_ONLY_OFFSET_SENSITIVITY_NOT_MEASURED_ERROR_DISTRIBUTION'})
    return output


def reference_signal(index, group, timestamp):
    """A separate direct interval query; no call to production lookup()."""
    index.finalize()
    if timestamp is None or not group or group in index.blocked_groups or index.unidentified_invalid_row:
        return 'UNKNOWN'
    data = index.groups.get(group)
    if data is None:
        return 'UNKNOWN'
    cursor = bisect_right(data['start'], timestamp) - 1
    matches = []
    while cursor >= 0 and data['max_end'][cursor] > timestamp:
        if data['start'][cursor] <= timestamp < data['end'][cursor]:
            matches.append(cursor)
        cursor -= 1
    if len(matches) > 1:
        return 'SIGNAL_AMBIGUOUS'
    if not matches:
        return 'UNKNOWN'
    state = STATES[data['state'][matches[0]]]
    return state if state != 'unknown' else 'UNKNOWN'


def audit_cases(selected, reconstructed, mappings, signals, saved_contexts):
    reference = {row['event_key']: row for row in reconstructed}
    output = []
    fields = ('event_id', 'scenario_id', 'pet_s', *TIMES, 'conflict_lon_deg', 'conflict_lat_deg',
              'pet_eligible', 'valid_nonnegative_pet', 'qc_status', 'invalid_reasons',
              *(role + '_' + field for role in ROLES for field in ('object_id', 'class', 'object_join_status', 'length_m', 'width_m')))
    for saved in selected:
        raw = reference.get(saved['event_key'])
        mismatches = []
        if raw is None:
            mismatches.append('SOURCE_EVENT_KEY_MISSING')
        else:
            for field in fields:
                left, right = saved.get(field), raw.get(field)
                if isinstance(left, (float, int)) and not isinstance(left, bool) and isinstance(right, (float, int)):
                    equal = math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-9)
                else:
                    equal = left == right
                if not equal:
                    mismatches.append('SOURCE_LEDGER_DIFFERENCE_' + field)
        numerator = saved.get('ts_enter_priority_ms')
        subtraction = saved.get('ts_leave_encroaching_ms')
        formula = (numerator - subtraction) / 1000 if numerator is not None and subtraction is not None else None
        difference = saved.get('pet_s') - formula if formula is not None and saved.get('pet_s') is not None else None
        if difference is not None and abs(difference) > 1e-6:
            mismatches.append('PROVIDER_TIMESTAMP_FORMULA_DISAGREEMENT')
        row = {field: saved.get(field) for field in ('event_key', 'event_id', 'scenario_id', 'pet_s', *TIMES, 'conflict_lon_deg', 'conflict_lat_deg', 'qc_status', 'invalid_reasons', 'source_refs')}
        row.update(selection_reasons=saved['selection_reasons'], timestamp_formula_pet_s=formula,
                   provider_minus_timestamp_pet_s=difference, source_event_found=raw is not None)
        mapping = mappings.get(saved.get('scenario_id'))
        for role in ROLES:
            group = mapping['raw'].get(role + '_signal_group_id') if mapping and not mapping['conflict'] else None
            state = reference_signal(signals, group, saved.get('ts_enter_' + role + '_ms'))
            existing = saved_contexts.get((saved['event_key'], role))
            row[role + '_reference_signal_state'] = state
            row[role + '_saved_signal_state'] = existing.get('signal_state') if existing else None
            row[role + '_signal_group_id'] = group
            row[role + '_object_join_status'] = saved.get(role + '_object_join_status')
            row[role + '_object_id'] = saved.get(role + '_object_id')
            if existing is None or state != existing.get('signal_state') or group != existing.get('signal_group_id'):
                mismatches.append(role.upper() + '_SIGNAL_SOURCE_CONTEXT_DIFFERENCE')
        row.update(mismatch_reasons=sorted(set(mismatches)), audit_status='CONSISTENT' if not mismatches else 'DIFFERENCE_REQUIRES_REVIEW')
        output.append(row)
    return output


def assess_frozen_model(rows, assignments, model, validation):
    """Feature ablation reuses frozen coordinates/centers, with zero refits.

    Centroid contributions are a geometric description, not predictive feature
    importance. Majority comparisons describe cluster labels, not ground truth.
    """
    import numpy as np
    from sklearn.metrics import adjusted_rand_score
    features = model.get('feature_names', [])
    if not features or not model.get('cluster_centers'):
        return {'status': 'NOT_APPLICABLE', 'reason': 'NO_SAVED_FITTED_MODEL', 'additional_fit_calls': 0}, {}
    allowed = {'encroaching_occupancy_s', 'priority_occupancy_s', 'encroaching_length_m', 'encroaching_width_m', 'priority_length_m', 'priority_width_m'}
    if not set(features) <= allowed:
        raise ContractError('FINISH_SAVED_MODEL_FEATURE_CONTRACT')
    kept = [row for row in rows if row['event_key'] in assignments]
    if not kept:
        raise ContractError('FINISH_AI_ASSIGNMENTS_WITHOUT_FEATURE_ROWS')
    raw = np.array([[finite(row.get(feature)) if finite(row.get(feature)) is not None else np.nan for feature in features] for row in kept])
    transformed = raw.copy()
    for j, feature in enumerate(features):
        if feature.endswith('occupancy_s'):
            transformed[:, j] = np.where(transformed[:, j] >= 0, np.log1p(transformed[:, j]), np.nan)
        else:
            transformed[:, j] = np.where(transformed[:, j] > 0, transformed[:, j], np.nan)
    medians = np.array(model['training_medians'])
    center = np.array(model['scaler_center'])
    scale = np.array(model['scaler_scale'])
    centers = np.array(model['cluster_centers'])
    if any(len(values) != len(features) for values in (medians, center, scale)) or centers.shape[1] != len(features) or np.any(scale <= 0):
        raise ContractError('FINISH_SAVED_MODEL_DIMENSIONS')
    x = (np.where(np.isfinite(transformed), transformed, medians) - center) / scale
    distances = ((x[:, None, :] - centers[None, :, :]) ** 2)
    predicted = distances.sum(axis=2).argmin(axis=1)
    saved = np.array([int(assignments[row['event_key']]['cluster']) for row in kept])
    if np.any(saved != predicted):
        raise ContractError('FINISH_FROZEN_ASSIGNMENT_RECONSTRUCTION_MISMATCH')
    contributions = np.zeros(len(features))
    for a in range(len(centers)):
        for b in range(a + 1, len(centers)):
            contributions += (centers[a] - centers[b]) ** 2
    total = float(contributions.sum())
    share_table = [{'feature': feature, 'feature_group': 'OWN_ROLE_DURATION' if feature.endswith('occupancy_s') else 'STATIC_OBJECT_DIMENSION',
                    'centroid_squared_separation': float(contributions[j]), 'centroid_separation_fraction': ratio(float(contributions[j]), total)} for j, feature in enumerate(features)]
    subset_table = []
    for name, columns in [('STATIC_OBJECT_DIMENSION', [j for j, f in enumerate(features) if not f.endswith('occupancy_s')]),
                          ('OWN_ROLE_DURATION', [j for j, f in enumerate(features) if f.endswith('occupancy_s')])]:
        if columns:
            subset_labels = distances[:, :, columns].sum(axis=2).argmin(axis=1)
            subset_table.append({'comparison': name, 'n_events': len(kept), 'n_same_saved_cluster': int((subset_labels == saved).sum()),
                                 'agreement_fraction': float((subset_labels == saved).mean()), 'ari': float(adjusted_rand_score(saved, subset_labels)),
                                 'interpretation': 'FROZEN_CENTER_DISTANCE_SUBSET_DIAGNOSTIC_NOT_NEW_FITTED_MODEL'})
    conditions = []
    baseline = []
    for kind in ('SCENARIO', 'ROAD_USER_PAIR', 'SCENARIO_AND_ROAD_USER_PAIR'):
        groups = defaultdict(Counter)
        for row, label in zip(kept, saved):
            pair = str(row.get('encroaching_class')) + '|' + str(row.get('priority_class'))
            key = str(row.get('scenario_id')) if kind == 'SCENARIO' else pair if kind == 'ROAD_USER_PAIR' else str(row.get('scenario_id')) + '|' + pair
            groups[key][int(label)] += 1
        agreement = sum(max(counts.values()) for counts in groups.values())
        baseline.append({'comparison': kind, 'n_events': len(kept), 'n_majority_cluster_agreement': agreement,
                         'agreement_fraction': ratio(agreement, len(kept)), 'evaluation_scope': 'SAME_COHORT_DESCRIPTIVE_NOT_HELDOUT_ACCURACY'})
        for key, counts in sorted(groups.items()):
            for cluster, count in sorted(counts.items()):
                conditions.append({'condition_type': kind, 'condition': key, 'cluster': cluster, 'n_events': count,
                                   'n_condition_events': sum(counts.values()), 'cluster_fraction': ratio(count, sum(counts.values()))})
    tails = []
    for j, feature in enumerate(features):
        observed = raw[np.isfinite(raw[:, j]), j]
        if not len(observed):
            continue
        q1, q3 = np.quantile(observed, [.25, .75]); iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        for cluster in sorted(set(saved.tolist())):
            values = raw[saved == cluster, j]; values = values[np.isfinite(values)]
            tails.append({'feature': feature, 'cluster': cluster, 'n_events_with_value': len(values),
                          'median': float(np.median(values)) if len(values) else None,
                          'minimum': float(values.min()) if len(values) else None, 'maximum': float(values.max()) if len(values) else None,
                          'reference_q1': float(q1), 'reference_q3': float(q3), 'iqr_lower_fence': float(lo), 'iqr_upper_fence': float(hi),
                          'n_outside_iqr_fences': int(((values < lo) | (values > hi)).sum()),
                          'meaning': 'DISTRIBUTIONAL_TAIL_NOT_CONFIRMED_ERROR'})
    size_share = sum(row['centroid_separation_fraction'] or 0 for row in share_table if row['feature_group'] == 'STATIC_OBJECT_DIMENSION')
    result = {'status': 'COMPUTED_FROM_FROZEN_MODEL', 'additional_fit_calls': 0, 'n_saved_assignment_rows_reviewed': len(kept),
              'assignment_reconstruction_mismatches': 0, 'selected_k': len(centers), 'static_dimension_centroid_separation_fraction': size_share,
              'diagnostic_conclusion': 'STATIC_SIZE_DOMINATES_CENTROID_SEPARATION' if size_share >= .8 else 'MIXED_OCCUPANCY_AND_SIZE_CENTROID_SEPARATION',
              'incremental_explanation_status': 'NOT_ESTABLISHED_BY_DESCRIPTIVE_CLUSTER_COMPARISONS',
              'measurement_outlier_confirmation': 'NOT_PERFORMED', 'independent_accuracy': None,
              'heldout_metrics': validation.get('heldout_metrics'), 'original_validation_status': validation.get('status'),
              'training_population': validation.get('preprocessing', {}).get('n_train'),
              'seed_stability': validation.get('stability'),
              'limitations': ['Feature subset diagnostics reuse frozen centers and scaling; they are not separately trained baselines.',
                              'IQR fences identify distributional tails; long or wide vehicles are not automatically erroneous.',
                              'Cluster agreement and silhouette do not establish accident risk, legal violation, or additional policy benefit.']}
    return result, {'ai_feature_separation': share_table, 'ai_frozen_subset_comparison': subset_table,
                    'ai_basic_description_comparison': baseline, 'ai_condition_counts': conditions, 'ai_feature_tail_description': tails}


def _verified_json(root, run, name, provenance):
    path = child(root, (run / name).relative_to(root).as_posix())
    entry = provenance.get('files', {}).get(name)
    if not entry or file_hash(path) != entry.get('sha256') or path.stat().st_size != entry.get('bytes'):
        raise ContractError('FINISH_INPUT_ARTIFACT_HASH_MISMATCH_' + name)
    return json.loads(path.read_text(encoding='utf-8'))


def _verify_csv(root, run, name, provenance):
    path = child(root, (run / name).relative_to(root).as_posix())
    entry = provenance.get('files', {}).get(name)
    if not entry or file_hash(path) != entry.get('sha256') or path.stat().st_size != entry.get('bytes'):
        raise ContractError('FINISH_INPUT_ARTIFACT_HASH_MISMATCH_' + name)
    return path


def _figures(directory, tables):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['svg.hashsalt'] = 'SPL_FINISH_EVIDENCE_V1'
    out = directory / 'figures'; out.mkdir(exist_ok=False)
    entries = []
    for name, rows, title, xlabel, ylabel, xfield, yfield in [
        ('audit_denominators', tables['audit_denominators'], 'Recorded interaction filters: explicit populations', 'Filter metric', 'Number of interactions', 'metric_id', 'numerator'),
        ('ai_feature_separation', tables.get('ai_feature_separation', []), 'Frozen cluster center separation by feature', 'Allowed feature', 'Fraction of squared centroid separation', 'feature', 'centroid_separation_fraction')]:
        if not rows:
            continue
        fig, ax = plt.subplots(figsize=(10, 5)); ax.bar(range(len(rows)), [row[yfield] or 0 for row in rows])
        ax.set_xticks(range(len(rows)), [row[xfield] for row in rows], rotation=25, ha='right', fontsize=8)
        ax.set(title=title, xlabel=xlabel, ylabel=ylabel); fig.tight_layout()
        files = []
        for suffix in ('png', 'svg'):
            path = out / (name + '.' + suffix); fig.savefig(path, dpi=160, metadata={'Date': None} if suffix == 'svg' else None)
            files.append({'path': path.relative_to(directory).as_posix(), 'sha256': file_hash(path), 'bytes': path.stat().st_size})
        plt.close(fig)
        source = directory / (name + '.csv')
        entries.append({'figure_id': name.upper(), 'files': files, 'source_table': source.name,
                        'source_table_sha256': file_hash(source), 'axes': {'x': xlabel, 'y': ylabel},
                        'limitations': 'Recorded interactions and frozen-model description; not traffic exposure or independent accuracy.'})
    write_json(directory / 'figures_index.json', {'figures': entries})


def run_evidence(root, pet_run, output, archives=None):
    root = verify_root(root); run = child(root, pet_run)
    if len(Path(pet_run).parts) != 2 or Path(pet_run).parts[0] != 'runs' or not run.is_dir():
        raise ContractError('FINISH_INPUT_MUST_BE_EXISTING_PROJECT_RUN')
    output_path = child(root, output)
    if len(Path(output).parts) != 2 or Path(output).parts[0] != 'runs':
        raise ContractError('FINISH_OUTPUT_MUST_BE_NEW_RUN')
    # Check collisions before parsing archives. Failed reserved attempts are retained.
    if output_path.exists():
        raise FileExistsError(output)
    provenance_path = child(root, pet_run + '/provenance.json')
    original_provenance = json.loads(provenance_path.read_text(encoding='utf-8'))
    inventory = _verified_json(root, run, 'source_inventory.json', original_provenance)
    result = _verified_json(root, run, 'pet_result.json', original_provenance)
    qc = _verified_json(root, run, 'pet_qc.json', original_provenance)
    ledger_path = _verify_csv(root, run, 'event_ledger.csv', original_provenance)
    context_path = _verify_csv(root, run, 'signal_context.csv', original_provenance)
    sensitivity_path = _verify_csv(root, run, 'timing_sensitivity.csv', original_provenance)
    sensitivity = clock_review(csv_rows(sensitivity_path))
    selected, counts, period = select_cases(csv_rows(ledger_path))
    threshold_path = _verify_csv(root, run, 'pet_thresholds.csv', original_provenance)
    saved_thresholds = {(row['scenario_id'], finite(row.get('tau_s'))): row for row in csv_rows(threshold_path)}
    for row in counts['scenario_threshold_counts']:
        existing = saved_thresholds.get((row['scenario_id'], row['tau_s']))
        if not existing or integer(existing.get('n_short_pet')) != row['n_short_pet'] or integer(existing.get('n_valid_nonnegative_pet')) != row['n_valid_nonnegative_pet']:
            raise ContractError('FINISH_SCENARIO_THRESHOLD_RECONCILIATION')
    selected_keys = {row['event_key'] for row in selected}
    saved_contexts = {(row['event_key'], row['role']): row for row in csv_rows(context_path)
                      if row['event_key'] in selected_keys and finite(row.get('clock_offset_s')) == 0}
    inputs = []
    overrides = list(archives or [])
    if overrides and len(overrides) != len(inventory['inputs']):
        raise ContractError('FINISH_ARCHIVE_OVERRIDE_COUNT')
    for index, specification in enumerate(inventory['inputs']):
        source = dict(specification); source['archive'] = overrides[index] if overrides else source['archive']
        references = [entry for entry in inventory['tables'] if entry['archive'] == specification['archive']]
        source['tables'] = [entry['table'] for entry in references]
        source['members'] = {entry['table']: entry['member'] for entry in references}
        inputs.append(source)
    profile = {'dataset_id': result.get('dataset_id', selected[0].get('dataset_id') if selected else 'sitpet'),
               'source_version': result.get('source_version', selected[0].get('source_version') if selected else '1.1'),
               'data_kind': result.get('data_kind'), 'scope': result.get('scope'), 'local_timezone': 'Europe/Vienna', 'inputs': inputs}
    event_ids = {row.get('event_id') for row in selected}
    needed_objects = {row.get(role + '_object_id') for row in selected for role in ROLES}
    tables = {name: [] for name in ('pet_events', 'objects', 'scenarios', 'scenario_signal_mapping')}
    signals = SignalIndex(); raw_counts = Counter(); raw_min = raw_max = None
    def sink(table, row):
        nonlocal raw_min, raw_max
        if table == 'pet_events':
            raw_counts['n_raw'] += 1; pet = finite(row.get('pet_s'))
            raw_counts['finite_pet'] += pet is not None
            raw_counts['finite_negative'] += pet is not None and pet < 0
            raw_counts['finite_nonnegative'] += pet is not None and pet >= 0
            times = [integer(row.get(field)) for field in TIMES]
            for timestamp in times:
                if timestamp is not None:
                    raw_min = timestamp if raw_min is None else min(raw_min, timestamp)
                    raw_max = timestamp if raw_max is None else max(raw_max, timestamp)
            difference = pet - (times[2] - times[1]) / 1000 if pet is not None and times[2] is not None and times[1] is not None else None
            raw_counts['timestamp_formula_checked'] += difference is not None
            raw_counts['timestamp_formula_disagreements'] += difference is not None and abs(difference) > 1e-6
            if key_value(row.get('event_id'), True) in event_ids:
                tables[table].append(row)
        elif table == 'objects':
            if key_value(row.get('object_id'), True) in needed_objects:
                tables[table].append(row)
        elif table == 'signal_states':
            source = row['__source']; signals.add(row, source['source_index'], source['source_row_id'])
        else:
            tables[table].append(row)
    _, _, actual_inventory = load_tables(root, profile, sink)
    expected_members = [(row['table'], row['source_file_sha256'], row['row_count']) for row in inventory['tables']]
    actual_members = [(row['table'], row['source_file_sha256'], row['row_count']) for row in actual_inventory['tables']]
    if expected_members != actual_members:
        raise ContractError('FINISH_SOURCE_TABLE_INVENTORY_MISMATCH')
    reconstructed, _, mappings, _ = build_ledger(tables, profile, actual_inventory)
    audited = audit_cases(selected, reconstructed, mappings, signals, saved_contexts)
    for key, expected in [('n_raw', raw_counts['n_raw']), ('n_valid_nonnegative_pet', counts.get('VALID_NONNEGATIVE_PET', 0)),
                          ('n_negative_pet', counts.get('NEGATIVE_PET_REVIEW', 0)), ('n_invalid', counts.get('INVALID', 0))]:
        if qc.get(key) != expected:
            raise ContractError('FINISH_DENOMINATOR_RECONCILIATION_' + key)
    period.update(source_scope=result.get('scope'), raw_earliest_timestamp_ms=raw_min, raw_latest_timestamp_ms=raw_max,
                  independent_observation_day_count=1 if result.get('scope') == 'SAMPLE_DAY' else None,
                  day_independence_status='ONE_PROVIDER_SAMPLE_DAY' if result.get('scope') == 'SAMPLE_DAY' else 'OBSERVED_CALENDAR_DATES_NOT_AUTOMATICALLY_INDEPENDENT',
                  independent_pet_accuracy=None, manual_review_status='NOT_PERFORMED')
    for prefix, timestamp in [('start', raw_min), ('end', raw_max)]:
        utc, _, local = event_time(timestamp)
        period[prefix + '_utc'] = utc; period[prefix + '_local'] = local.isoformat() if local else None
    analysis = {'status': 'NOT_APPLICABLE', 'additional_fit_calls': 0, 'reason': 'NO_SAVED_AI_MODEL'}; ai_tables = {}
    if 'cluster_model.json' in original_provenance.get('files', {}):
        model = _verified_json(root, run, 'cluster_model.json', original_provenance)
        validation = _verified_json(root, run, 'cluster_validation.json', original_provenance)
        assignment_path = _verify_csv(root, run, 'cluster_assignments.csv', original_provenance)
        assignments = {}
        for row in csv_rows(assignment_path):
            if row['event_key'] in assignments:
                raise ContractError('FINISH_DUPLICATE_AI_ASSIGNMENT')
            assignments[row['event_key']] = row
            if len(assignments) > 100000:
                raise ContractError('FINISH_AI_REVIEW_COHORT_EXCEEDS_100K_DECLARE_BOUNDED_COHORT')
        if assignments:
            feature_rows = [row for row in csv_rows(ledger_path) if row['event_key'] in assignments]
            if len(feature_rows) != len(assignments):
                raise ContractError('FINISH_MISSING_AI_FEATURE_ROWS')
            analysis, ai_tables = assess_frozen_model(feature_rows, assignments, model, validation)
    directory = new_run(root, output)
    denominators = denominator_rows(counts, raw_counts)
    audit = {'status': 'CONSISTENT' if all(row['audit_status'] == 'CONSISTENT' for row in audited) else 'DIFFERENCES_RECORDED',
             'method': 'AUTOMATED_NUMERICAL_SOURCE_CONSISTENCY_CHECK', 'selection_rule': SELECTION_RULE,
             'selected_cases': len(audited), 'negative_cases_selected': sum('ALL_FINITE_NEGATIVE_PET' in row['selection_reasons'] for row in audited),
             'short_1s_cases_selected': sum('ALL_QC_VALID_0_TO_1S' in row['selection_reasons'] for row in audited),
             'n_cases_with_differences': sum(row['audit_status'] != 'CONSISTENT' for row in audited),
             'raw_source_counts': dict(sorted(raw_counts.items())), 'ledger_counts': counts,
             'independent_accuracy': None, 'manual_review_status': 'NOT_PERFORMED',
             'limitations': ['Shared provider-table parser and production QC are reused for role joins; timestamp arithmetic and interval query are separately calculated.',
                             'Timestamp agreement does not independently verify provider geometric PET construction or original interaction selection.']}
    all_tables = {'event_audit_cases': audited, 'audit_denominators': denominators,
                  'audit_scenario_thresholds': counts['scenario_threshold_counts'], 'audit_clock_sensitivity': sensitivity, **ai_tables}
    for name, rows in all_tables.items():
        write_table(directory, name, rows)
    for name, value in [('event_audit', audit), ('ai_assessment', analysis), ('sample_scope', period), ('calculation_formulae', FORMULAE)]:
        write_json(directory / (name + '.json'), value)
    _figures(directory, all_tables)
    facts = [dict(row, value=row['fraction'], formula_id='conditional_short_fraction' if row['metric_id'] == 'valid_nonnegative_pet_le_1s' else 'explicit_count_over_declared_population',
                  input_run=pet_run, input_refs=actual_inventory['inputs'], output_ref='audit_denominators.csv') for row in denominators]
    for metric, value in [('raw_provider_rows', raw_counts['n_raw']), ('unique_reconciled_events', counts['unique_ledger_rows']),
                          ('qc_valid_nonnegative_events', counts.get('VALID_NONNEGATIVE_PET', 0)),
                          ('qc_valid_negative_events', counts.get('NEGATIVE_PET_REVIEW', 0)), ('qc_invalid_events', counts.get('INVALID', 0))]:
        facts.append({'metric_id': metric, 'value': value, 'numerator': value, 'denominator': None, 'unit': 'interaction count',
                      'population': 'Raw provider rows' if metric == 'raw_provider_rows' else 'Unique reconciled interaction keys',
                      'formula_id': 'explicit_count', 'input_run': pet_run, 'output_ref': 'event_audit.json'})
    for row in counts['scenario_threshold_counts']:
        facts.append({'metric_id': 'scenario_short_fraction_' + row['scenario_id'] + '_' + str(row['tau_s']),
                      'value': row['short_pet_fraction'], 'numerator': row['n_short_pet'], 'denominator': row['n_valid_nonnegative_pet'],
                      'unit': 'recorded interaction fraction', 'population': 'QC-valid nonnegative provider interactions in scenario ' + row['scenario_id'],
                      'formula_id': 'conditional_short_fraction', 'input_run': pet_run, 'output_ref': 'audit_scenario_thresholds.csv'})
    separation_rows = ai_tables.get('ai_feature_separation', [])
    dimension_sum = sum(row['centroid_squared_separation'] for row in separation_rows if row['feature_group'] == 'STATIC_OBJECT_DIMENSION')
    separation_sum = sum(row['centroid_squared_separation'] for row in separation_rows)
    facts.append({'metric_id': 'static_dimension_centroid_separation_fraction', 'value': analysis.get('static_dimension_centroid_separation_fraction'),
                  'numerator': dimension_sum if separation_rows else None, 'denominator': separation_sum if separation_rows else None, 'unit': 'fraction of squared frozen centroid separation',
                  'formula_id': 'centroid_feature_share', 'population': 'Saved bounded AI assignment cohort', 'input_run': pet_run,
                  'output_ref': 'ai_feature_separation.csv' if ai_tables else None, 'additional_fit_calls': 0})
    for record in facts:
        record.update(code_ref='src/spl/finish_evidence.py', code_sha256=file_hash(root / 'src/spl/finish_evidence.py'),
                      config_ref=original_provenance.get('config_ref'), config_sha256=original_provenance.get('config_sha256'),
                      formula=FORMULAE.get(record['formula_id'], 'Count rows in the explicitly declared population'),
                      input_refs=actual_inventory['inputs'], input_run_provenance_sha256=file_hash(provenance_path),
                      output_sha256=file_hash(directory / record['output_ref']) if record.get('output_ref') else None)
    with (directory / 'metric_facts.jsonl').open('x', encoding='utf-8', newline='\n') as stream:
        for record in facts:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n')
    technical = {'data_kind': result.get('data_kind'), 'scope': result.get('scope'), 'input_run': pet_run,
                 'event_audit_status': audit['status'], 'additional_fit_calls': 0, 'denominators': denominators,
                 'ai_diagnostic_conclusion': analysis.get('diagnostic_conclusion'), 'ai_incremental_benefit_status': analysis.get('incremental_explanation_status'),
                 'manual_review_status': 'NOT_PERFORMED', 'independent_accuracy': None,
                 'claim_limits': ['Recorded interaction fractions are not vehicle exposure, accident probability or legal violation accuracy.',
                                  'Sparse short-PET cases do not support general policy priority or transfer to a domestic intersection.' if result.get('scope') == 'SAMPLE_DAY' else 'Single-intersection recorded interactions do not establish general policy priority or transfer to a domestic intersection.']}
    write_json(directory / 'technical_summary.json', technical)
    sys.path.insert(0, str(root / 'tools'))
    from snapshot import snapshot, environment_manifest_ref
    evidence_source = snapshot(); env_ref = environment_manifest_ref(root)
    canonical = sorted(path.relative_to(directory).as_posix() for path in directory.rglob('*') if path.is_file() and path.suffix in ('.csv', '.parquet', '.json', '.jsonl') and path.name != 'figures_index.json')
    hashes = {name: {'sha256': file_hash(directory / name), 'bytes': (directory / name).stat().st_size} for name in canonical}
    evidence_provenance = {'input_run': pet_run, 'input_run_provenance_sha256': file_hash(provenance_path),
                           'input_run_source_snapshot': original_provenance.get('source_snapshot'),
                           'source_snapshot': evidence_source, 'config_ref': original_provenance.get('config_ref'),
                           'config_sha256': original_provenance.get('config_sha256'), 'environment_ref': env_ref,
                           'environment_sha256': file_hash(child(root, env_ref)), 'requirements_lock_sha256': file_hash(root / 'requirements.lock'),
                           'inputs': actual_inventory['inputs'], 'files': hashes, 'additional_fit_calls': 0,
                           'raw_payload_exported': False, 'manual_review_status': 'NOT_PERFORMED',
                           'execution_command': sys.argv, 'canonical_files': canonical}
    write_json(directory / 'provenance.json', evidence_provenance)
    write_json(directory / 'run_manifest.json', {'profile_id': 'PET_FINISH_EVIDENCE_V1', 'canonical_files': canonical, 'additional_fit_calls': 0})
    return technical
