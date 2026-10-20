"""Disk-backed full PET baseline with a separately declared bounded AI cohort."""
from array import array
from collections import Counter, defaultdict
import csv
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

from .pet_artifacts import flattened, write_json, write_table, figures
from .pet_contracts import TIMES
from .pet_io import load_profile, file_hash
from .pet_qc import build_ledger
from .pet_signals import contexts
from .pet_metrics import sensitivity_rows
from .pet_run import source_snapshot, ai_status
from .pet_ai import FEATURES, analyze_types, count_actual_fit_calls
from .safety import verify_root, child, new_run


def export_event(row):
    exported = {key: value for key, value in row.items() if key not in {'provider_fields', 'conflicting_source_variants'}}
    variants = row['conflicting_source_variants']
    exported['n_conflicting_source_variants'] = len(variants)
    exported['conflicting_field_names'] = sorted({key for variant in variants for key, value in variant.items() if row['provider_fields'].get(key) != value})
    exported['invalid_original_values'] = {key: row['provider_fields'].get(key) for key in ('event_id', 'pet_s', 'encroaching_object_id', 'priority_object_id', 'conflict_lon_deg', 'conflict_lat_deg', *TIMES) if row.get(key) is None}
    if row.get('event_id') is None:
        origin = row['source_refs'][0]
        exported['event_key'] = row['dataset_id'] + '/' + row['source_version'] + '/INVALID_KEY_' + str(origin['source_index']) + ':' + str(origin['source_row_id'])
    return exported


class StreamingTable:
    """Exclusive CSV plus fixed-size Parquet shards; bounded pending rows."""
    def __init__(self, directory, name, shard_size=25000):
        self.directory = Path(directory); self.name = name; self.limit = shard_size
        self.csv = (self.directory / (name + '.csv')).open('x', encoding='utf-8', newline='')
        self.shard_dir = self.directory / (name + '_parquet'); self.shard_dir.mkdir(exist_ok=False)
        self.fields = None; self.pending = []; self.shards = []; self.count = 0

    def add(self, rows):
        for row in rows:
            if self.fields is None:
                self.fields = sorted(row)
                self.writer = csv.DictWriter(self.csv, fieldnames=self.fields, lineterminator='\n')
                self.writer.writeheader()
            if set(row) != set(self.fields):
                raise ValueError('Streaming table fields changed: ' + self.name)
            value = flattened(row); self.writer.writerow(value)
            # Parquet scalar strings mirror the lossless stable CSV representation.
            # This avoids all-null batches silently changing per-shard schemas.
            self.pending.append({key: '' if value[key] is None else str(value[key]) for key in self.fields})
            self.count += 1
            if len(self.pending) >= self.limit:
                self.flush()

    def flush(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        if not self.pending:
            return
        path = self.shard_dir / ('part_%05d.parquet' % len(self.shards))
        schema = pa.schema([(field, pa.string()) for field in self.fields])
        with path.open('xb') as stream:
            pq.write_table(pa.Table.from_pylist(self.pending, schema=schema), stream, compression='zstd')
        self.shards.append({'path': path.relative_to(self.directory).as_posix(), 'row_count': len(self.pending)})
        self.pending.clear()

    def close(self):
        if self.fields is None:
            self.csv.write('status\n')
        self.flush(); self.csv.close()
        write_json(self.directory / (self.name + '_parquet_index.json'),
                   {'table': self.name, 'row_count': self.count, 'shards': self.shards,
                    'scalar_encoding': 'STABLE_CSV_SCALAR_STRINGS_WITH_COLUMN_NAMES',
                    'nested_columns': 'STABLE_JSON_STRINGS', 'numeric_boolean_types': 'Decode by PET schema or CSV headers; no lossy float conversion'})


def sorted_quantile(values, q):
    if not values:
        return None
    position = (len(values) - 1) * q; lo = math.floor(position); hi = math.ceil(position)
    return values[lo] + (position - lo) * (values[hi] - values[lo])


class AggregateStore:
    """Compact scalar SQL facts preserve exact counts and quantiles, not raw rows."""
    def __init__(self, path, thresholds=(.5, 1, 1.5, 2)):
        self.path = Path(path)
        if self.path.exists():
            raise FileExistsError(self.path)
        self.db = sqlite3.connect(self.path)
        self.db.executescript('''PRAGMA journal_mode=OFF; PRAGMA cache_size=-32768; PRAGMA temp_store=FILE;
            CREATE TABLE facts(k TEXT PRIMARY KEY,scenario TEXT,pet REAL,status TEXT,nsource INTEGER,day TEXT,bin INTEGER,
                               eo TEXT,po TEXT,known INTEGER,ai TEXT);
        ''')
        self.thresholds = list(thresholds); self.invalid = Counter(); self.review = Counter(); self.timing = {}

    def add(self, ledger, context, sensitivity):
        paired = defaultdict(list)
        for row in context:
            paired[row['event_key']].append(row['signal_state'])
        columns = ('event_key', 'event_id', 'scenario_id', 'data_kind', 'event_date_local', 'event_date_utc',
                   'encroaching_object_id', 'priority_object_id', 'encroaching_class', 'priority_class', 'pet_s',
                   'pet_eligible', 'valid_nonnegative_pet', 'qc_status', *FEATURES)
        records = []
        for row in ledger:
            states = paired[row['event_key']]
            known = len(states) == 2 and all(state not in ('UNKNOWN', 'SIGNAL_AMBIGUOUS') for state in states)
            ai = json.dumps({field: row.get(field) for field in columns}, sort_keys=True, separators=(',', ':'), allow_nan=False)
            records.append((row['event_key'], row['scenario_id'] or 'UNKNOWN_SCENARIO', row['pet_s'], row['qc_status'],
                            row['n_source_rows'], row['event_date_local'], row['local_4h_bin'],
                            row['encroaching_object_id'], row['priority_object_id'], int(known), ai))
            self.invalid.update(row['invalid_reasons']); self.review.update(row['review_reasons'])
        self.db.executemany('INSERT INTO facts VALUES(?,?,?,?,?,?,?,?,?,?,?)', records); self.db.commit()
        for row in sensitivity:
            key = row['scenario_id'], row['clock_offset_s']
            if key not in self.timing:
                self.timing[key] = dict(row, n_valid_nonnegative_pet=0, n_signal_known=0, n_signal_ambiguous_or_unknown=0, role_state_pair_counts={})
            total = self.timing[key]
            for name in ('n_valid_nonnegative_pet', 'n_signal_known', 'n_signal_ambiguous_or_unknown'):
                total[name] += row[name]
            total['pet_values_unchanged'] = total['pet_values_unchanged'] and row['pet_values_unchanged']
            if row['max_abs_relative_timestamp_gap_change_s'] is not None:
                total['max_abs_relative_timestamp_gap_change_s'] = max(total['max_abs_relative_timestamp_gap_change_s'] or 0, row['max_abs_relative_timestamp_gap_change_s'])
            counts = Counter(total['role_state_pair_counts']); counts.update(row['role_state_pair_counts'])
            total['role_state_pair_counts'] = dict(sorted(counts.items()))

    def finish(self):
        self.db.executescript('CREATE INDEX fact_scenario ON facts(scenario,status,pet); CREATE INDEX fact_days ON facts(day,scenario); CREATE INDEX fact_bootstrap ON facts(scenario,day,status,pet);')

    def value(self, sql, parameters=()):
        return self.db.execute(sql, parameters).fetchone()[0]

    def metrics(self, scenario_ids):
        scenarios = sorted(set(scenario_ids) | {row[0] for row in self.db.execute('SELECT DISTINCT scenario FROM facts')})
        summaries = []; thresholds = []; candidates = []; bins = []
        for scenario in scenarios:
            values = array('d', (row[0] for row in self.db.execute("SELECT pet FROM facts WHERE scenario=? AND status='VALID_NONNEGATIVE_PET' ORDER BY pet", (scenario,))))
            n = len(values)
            counts = dict(self.db.execute('SELECT status,count(*) FROM facts WHERE scenario=? GROUP BY status', (scenario,)))
            known = self.value("SELECT count(*) FROM facts WHERE scenario=? AND status='VALID_NONNEGATIVE_PET' AND known=1", (scenario,))
            days = self.value("SELECT count(DISTINCT day) FROM facts WHERE scenario=? AND status='VALID_NONNEGATIVE_PET' AND day IS NOT NULL", (scenario,))
            bounds = self.db.execute('SELECT min(pet),max(pet) FROM facts WHERE scenario=?', (scenario,)).fetchone()
            summaries.append({'scenario_id': scenario, 'n_raw': self.value('SELECT coalesce(sum(nsource),0) FROM facts WHERE scenario=?', (scenario,)),
                              'n_unique_event_keys': sum(counts.values()), 'n_valid_nonnegative_pet': n, 'n_negative_pet': counts.get('NEGATIVE_PET_REVIEW', 0),
                              'n_invalid': counts.get('INVALID', 0), 'n_zero_pet': sum(value == 0 for value in values),
                              'n_signal_known': known, 'n_signal_ambiguous_or_unknown': n - known, 'n_observation_days': days,
                              'unique_object_count': self.value('SELECT count(*) FROM (SELECT eo AS id FROM facts WHERE scenario=? UNION SELECT po FROM facts WHERE scenario=?) WHERE id IS NOT NULL', (scenario, scenario)),
                              'unique_ordered_pair_count': self.value('SELECT count(*) FROM (SELECT eo,po FROM facts WHERE scenario=? GROUP BY eo,po)', (scenario,)),
                              'pet_raw_min_s': bounds[0], 'pet_raw_max_s': bounds[1],
                              **{'pet_nonnegative_q' + str(int(q * 100)).zfill(2) + '_s': sorted_quantile(values, q) for q in (.05, .25, .5, .75, .95)},
                              'signal_count_population': 'VALID_NONNEGATIVE_PET_INTERACTIONS', 'population_definition': 'Recorded valid nonnegative provider PET interactions'})
            for tau in self.thresholds:
                short = sum(value <= tau for value in values)
                known_short = self.value("SELECT count(*) FROM facts WHERE scenario=? AND status='VALID_NONNEGATIVE_PET' AND known=1 AND pet<=?", (scenario, tau))
                thresholds.append({'scenario_id': scenario, 'tau_s': tau, 'n_short_pet': short, 'n_valid_nonnegative_pet': n,
                                   'short_pet_fraction': short / n if n else None, 'n_signal_known': known, 'n_short_pet_signal_known': known_short,
                                   'short_pet_fraction_signal_known': known_short / known if known else None,
                                   'conditional_denominator_definition': 'Recorded valid nonnegative PET interactions; signal subset requires both role states known',
                                   'tau_basis': 'OPERATIONAL_COMPARISON_NOT_LEGAL_OR_ACCIDENT_THRESHOLD'})
            candidates.append({'scenario_id': scenario, 'n_valid_nonnegative_pet': n, 'n_observation_days': days, 'candidate_status': 'DESCRIPTIVE_ONLY',
                               'rank': None, 'population_ci_95': None, 'rank_reason': 'EXPLORATORY_THRESHOLD_RANGE_ONLY',
                               'claim_scope': 'Observed scenario-condition inspection candidate, not city safety priority'})
            for start, in self.db.execute("SELECT DISTINCT bin FROM facts WHERE scenario=? AND status='VALID_NONNEGATIVE_PET' AND bin IS NOT NULL ORDER BY bin", (scenario,)):
                selected = array('d', (row[0] for row in self.db.execute("SELECT pet FROM facts WHERE scenario=? AND status='VALID_NONNEGATIVE_PET' AND bin=? ORDER BY pet", (scenario, start))))
                bins.append({'scenario_id': scenario, 'local_4h_start': start, 'local_4h_end': start + 4, 'timezone': 'Europe/Vienna',
                             'n_valid_nonnegative_pet': len(selected), 'pet_median_s': sorted_quantile(selected, .5)})
        return {'scenario_summary': summaries, 'pet_thresholds': thresholds, 'inspection_candidates': candidates, 'timebin_summary': bins}

    def ecdf_rows(self):
        cumulative = 0; previous = None; denominator = 0
        for scenario, pet, count in self.db.execute("SELECT scenario,pet,count(*) FROM facts WHERE status='VALID_NONNEGATIVE_PET' GROUP BY scenario,pet ORDER BY scenario,pet"):
            if scenario != previous:
                cumulative = 0; previous = scenario
                denominator = self.value("SELECT count(*) FROM facts WHERE scenario=? AND status='VALID_NONNEGATIVE_PET'", (scenario,))
            cumulative += count
            yield {'scenario_id': scenario, 'pet_s': pet, 'count_at_value': count, 'cumulative_count': cumulative,
                   'n_valid_nonnegative_pet': denominator, 'ecdf': cumulative / denominator}

    def ecdf_display(self):
        # The complete ECDF stays on disk; at most 256 display values per scenario.
        result = []; scenario = None; count = 0; wanted = set()
        for row in self.ecdf_rows():
            if row['scenario_id'] != scenario:
                scenario = row['scenario_id']; count = 0
                size = self.value("SELECT count(DISTINCT pet) FROM facts WHERE scenario=? AND status='VALID_NONNEGATIVE_PET'", (scenario,))
                wanted = {round(i * (size - 1) / min(255, size - 1)) for i in range(min(256, size))} if size > 1 else {0}
            if count in wanted:
                result.append(row)
            count += 1
        return result

    def ai_sample(self, cap=100000, seed=20261012):
        cap = min(100000, cap)
        if cap < 1:
            raise ValueError('AI_INPUT_CAP_MUST_BE_POSITIVE')
        groups = [(day, scenario, count) for day, scenario, count in self.db.execute("SELECT day,scenario,count(*) FROM facts WHERE status!='INVALID' GROUP BY day,scenario ORDER BY day,scenario")]
        total = sum(count for _, _, count in groups)
        target = min(cap, total)
        quotas = {(day, scenario): math.floor(target * count / total) if total else 0 for day, scenario, count in groups}
        remaining = target - sum(quotas.values())
        order = sorted(groups, key=lambda item: (-(target * item[2] / total - quotas[item[:2]]), str(item[:2]))) if total else []
        for day, scenario, _ in order[:remaining]:
            quotas[day, scenario] += 1
        selected = []; quota_records = []
        for day, scenario, count in groups:
            limit = quotas[day, scenario]; heap = []
            if limit:
                for key, payload in self.db.execute("SELECT k,ai FROM facts WHERE status!='INVALID' AND day IS ? AND scenario=? ORDER BY k", (day, scenario)):
                    score = int.from_bytes(hashlib.sha256((str(seed) + '\0' + key).encode()).digest(), 'big')
                    item = (-score, key, payload)
                    if len(heap) < limit:
                        heapq.heappush(heap, item)
                    elif item > heap[0]:
                        heapq.heapreplace(heap, item)
                selected.extend(json.loads(item[2]) for item in heap)
            quota_records.append({'event_date_local': day, 'scenario_id': scenario, 'n_qc_valid_full_population': count, 'n_selected_ai_input': limit})
        selected.sort(key=lambda row: row['event_key'])
        return selected, {'method': 'DATE_SCENARIO_PROPORTIONAL_LARGEST_REMAINDER_QUOTAS_SHA256_SEED_EVENT_KEY_LOWEST_VALUES',
                          'seed': seed, 'n_full_qc_valid': total, 'n_ai_input': len(selected), 'cap': cap,
                          'selection_used_pet_or_signal_values': False, 'strata': quota_records,
                          'evaluation_scope': 'Bounded sample of recorded interactions; full baseline denominators unchanged'}

    def bootstrap(self, metrics, profile):
        import numpy as np
        settings = profile.get('bootstrap', {})
        if int(settings.get('repetitions', 200)) != 200 or int(settings.get('seed', 20261012)) != 20261012:
            raise ValueError('R1 bootstrap fixed at B200 seed20261012')
        dates = [row[0] for row in self.db.execute("SELECT DISTINCT day FROM facts WHERE status='VALID_NONNEGATIVE_PET' AND day IS NOT NULL ORDER BY day")]
        eligible = {row['scenario_id'] for row in metrics['scenario_summary'] if row['n_valid_nonnegative_pet'] >= int(settings.get('min_events', 30)) and row['n_observation_days'] >= int(settings.get('min_days', 7))}
        if profile.get('scope') == 'SAMPLE_DAY':
            eligible.clear()
        for row in metrics['inspection_candidates']:
            row.update(rank_min_across_thresholds=None, rank_max_across_thresholds=None, ci_reason='INSUFFICIENT_OBSERVATION_DAYS_OR_EVENTS',
                       independent_observation_scope='ONE_SOURCE_SAMPLE_DAY' if profile.get('scope') == 'SAMPLE_DAY' else 'RECORDED_LOCAL_EVENT_DATES_NOT_ASSUMED_INDEPENDENT_POPULATION')
        if not eligible:
            return [], {'status': 'NOT_APPLICABLE', 'reason': 'INSUFFICIENT_OBSERVATION_DAYS_OR_EVENTS', 'repetitions': 0}
        generator = np.random.default_rng(20261012); resamples = generator.integers(0, len(dates), size=(200, len(dates)))
        output = []
        for scenario in sorted(eligible):
            counts = np.asarray([self.value("SELECT count(*) FROM facts WHERE scenario=? AND day=? AND status='VALID_NONNEGATIVE_PET'", (scenario, day)) for day in dates])
            for tau in self.thresholds:
                short = np.asarray([self.value("SELECT count(*) FROM facts WHERE scenario=? AND day=? AND status='VALID_NONNEGATIVE_PET' AND pet<=?", (scenario, day, tau)) for day in dates])
                denom = counts[resamples].sum(axis=1); numer = short[resamples].sum(axis=1)
                fractions = numer[denom > 0] / denom[denom > 0]
                output.append({'scenario_id': scenario, 'tau_s': tau, 'n_valid_nonnegative_pet': int(counts.sum()), 'n_date_clusters': len(dates),
                               'n_valid_bootstrap_replicates': len(fractions), 'n_zero_denominator_replicates': int(sum(denom == 0)),
                               'short_pet_fraction': float(short.sum() / counts.sum()),
                               'conditional_date_cluster_interval_lower': float(np.quantile(fractions, .025)) if len(fractions) else None,
                               'conditional_date_cluster_interval_upper': float(np.quantile(fractions, .975)) if len(fractions) else None,
                               'interval_scope': 'EXPLORATORY_CONDITIONAL_ON_THIS_INTERSECTION_RECORDED_DATES_NOT_GENERALIZATION_CI', 'seed': 20261012, 'repetitions': 200})
        for candidate in metrics['inspection_candidates']:
            if candidate['scenario_id'] in eligible:
                ranks = []
                for tau in self.thresholds:
                    values = {row['scenario_id']: row['short_pet_fraction'] for row in output if row['tau_s'] == tau}
                    ranks.append(1 + sum(value > values[candidate['scenario_id']] for value in values.values()))
                candidate.update(candidate_status='ELIGIBLE_FOR_EXPLORATORY_MULTI_DAY_COMPARISON', ci_reason='SEE_CONDITIONAL_DATE_CLUSTER_INTERVAL_TABLE',
                                 rank_reason='THRESHOLD_SENSITIVITY_RANGE_WITH_TIES; NO_SINGLE_CITY_SAFETY_RANK',
                                 rank_min_across_thresholds=min(ranks) if len(eligible) > 1 else None,
                                 rank_max_across_thresholds=max(ranks) if len(eligible) > 1 else None)
        return output, {'status': 'COMPUTED_EXPLORATORY_DATE_CLUSTER_INTERVALS', 'repetitions': 200, 'seed': 20261012,
                        'eligible_scenarios': sorted(eligible), 'scope': 'This intersection observed date clusters only; no broader population CI'}

    def close(self):
        self.db.close()


def run_bulk(root, profile_relative, output, qc_only=False):
    from .pet_batch_io import prepare
    root = verify_root(root); target = child(root, output)
    if len(Path(output).parts) != 2 or Path(output).parts[0] != 'runs':
        raise ValueError('OUTPUT_MUST_BE_RUNS_NEW_ID')
    if target.exists():
        raise FileExistsError(target)
    name = Path(output).name
    cache_dir = child(root, '.cache'); cache_dir.mkdir(exist_ok=True)
    input_cache = child(root, '.cache/' + name + '_input.sqlite')
    metrics_cache = child(root, '.cache/' + name + '_metrics.sqlite')
    if input_cache.exists() or metrics_cache.exists():
        raise FileExistsError('Derived cache collision; use a fresh run ID')
    profile, rights, config_hash = load_profile(root, profile_relative)
    before, env_ref = source_snapshot(root); start = time.monotonic()
    directory = new_run(root, output)
    write_json(directory / 'analysis_request.json', {'profile_id': 'PET_INTERACTIONS_V1', 'config_ref': profile_relative,
               'config_sha256': config_hash, 'processing_mode': 'DATE_BATCHED_FULL_BASELINE_BOUNDED_AI', 'source_sha256': before['sha256']})
    store = aggregate = None; writers = []
    try:
        store = prepare(root, profile, input_cache)
        aggregate = AggregateStore(metrics_cache, profile.get('thresholds_s', [.5, 1, 1.5, 2]))
        ledger_writer = StreamingTable(directory, 'event_ledger'); context_writer = StreamingTable(directory, 'signal_context')
        writers = [ledger_writer, context_writer]; static_qc = None; scenarios = {}
        for batch_number, (tables, signals) in enumerate(store.batches(), 1):
            ledger, batch_qc, mappings, scenarios = build_ledger(tables, profile, store.inventory)
            if static_qc is None:
                static_qc = batch_qc
            exported = [export_event(row) for row in ledger]
            for original, value in zip(ledger, exported):
                original['event_key'] = value['event_key']
            context = contexts(ledger, mappings, signals)
            sensitivity = sensitivity_rows(ledger, mappings, signals, profile.get('clock_offsets_s', [-.5, -.2, 0, .2, .5]))
            aggregate.add(ledger, context, sensitivity)
            ledger_writer.add(exported); context_writer.add(context)
            print('FULL_BASELINE_BATCH ' + str(batch_number) + ' events=' + str(len(ledger)), flush=True)
        for writer in writers:
            writer.close()
        writers.clear(); aggregate.finish()
        qc = dict(static_qc or {})
        partitions = dict(aggregate.db.execute('SELECT status,count(*) FROM facts GROUP BY status'))
        unique = sum(partitions.values())
        event_rows = store.global_qc['event_rows']
        qc.update(event_rows=event_rows, object_rows=store.global_qc['object_rows'], signal_rows=store.global_qc['signal_rows'],
                  n_raw=event_rows['n_raw'], n_unique_event_keys=unique,
                  n_valid_nonnegative_pet=partitions.get('VALID_NONNEGATIVE_PET', 0), n_negative_pet=partitions.get('NEGATIVE_PET_REVIEW', 0),
                  n_invalid=partitions.get('INVALID', 0), n_zero_pet=aggregate.value("SELECT count(*) FROM facts WHERE status='VALID_NONNEGATIVE_PET' AND pet=0"),
                  invalid_reason_counts=dict(sorted(aggregate.invalid.items())), review_reason_counts=dict(sorted(aggregate.review.items())),
                  reconciliation={'unique_partition_preserved': unique == sum(partitions.values()),
                                  'raw_rows_preserved': event_rows['n_raw'] == unique + event_rows['n_identical_duplicate_rows_merged'] + event_rows['n_conflicting_duplicate_rows_extra']},
                  signal_count_population='VALID_NONNEGATIVE_PET_INTERACTIONS', processing_mode='DATE_BATCHED_FULL_BASELINE_BOUNDED_AI',
                  manual_review_status='NOT_PERFORMED', independent_pet_accuracy=None,
                  population_definition='Publisher-recorded interactions in fixed scenarios, not all traffic participants')
        qc['missing_static_key_counts'] = dict(qc.get('missing_static_key_counts', {}), objects=store.global_qc['object_rows']['n_missing_key_rows'])
        metrics = aggregate.metrics(scenarios)
        bootstrap, bootstrap_status = aggregate.bootstrap(metrics, profile)
        ecdf_writer = StreamingTable(directory, 'pet_ecdf')
        ecdf_writer.add(aggregate.ecdf_rows()); ecdf_writer.close()
        small_tables = {**metrics, 'timing_sensitivity': [value for _, value in sorted(aggregate.timing.items())], 'date_cluster_intervals': bootstrap}
        for table, rows in small_tables.items():
            write_table(directory, table, rows)
        sample, sample_audit = aggregate.ai_sample(min(100000, int(profile.get('ai', {}).get('input_sample_max_events', 100000))))
        write_table(directory, 'ai_population', sample); write_json(directory / 'ai_sampling.json', sample_audit)
        if not qc_only:
            ai, ai_tables = analyze_types(sample, profile.get('ai', {}), directory / 'fit_attempts.jsonl', profile.get('scope'), cumulative_counter=lambda: count_actual_fit_calls(root))
            for table, rows in ai_tables.items():
                write_table(directory, table, rows)
            if 'model' in ai:
                write_json(directory / 'cluster_model.json', ai['model'])
        else:
            ai = {'status': 'NOT_RUN', 'reason': 'PET_QC_ONLY_COMMAND', 'fit_call_count': 0, 'heldout_metrics': None}; ai_tables = {}
        ai['input_sampling'] = sample_audit
        ai['ai_population_scope'] = 'Bounded predeclared date-scenario sample; full baseline metrics use every reconciled provider interaction'
        plot_metrics = dict(metrics, pet_ecdf=aggregate.ecdf_display())
        figures(directory, plot_metrics, store.inventory['inputs'], ai, ai_tables)
        status = {'profile_id': 'PET_INTERACTIONS_V1', 'dataset_id': profile['dataset_id'], 'source_version': str(profile['source_version']),
                  'scope': profile.get('scope'), 'data_kind': profile.get('data_kind', 'REAL'), 'mode': 'pet-qc' if qc_only else 'pet-analyze',
                  'pet_qc_status': 'COMPLETED_PROVIDER_DERIVED_QC', 'pet_analysis_status': 'COMPLETED_DESCRIPTIVE_BASELINE',
                  'processing_mode': 'DATE_BATCHED_FULL_BASELINE_BOUNDED_AI',
                  'ai_status': ai_status(ai), 'ai_attempted': ai['fit_call_count'] > 0, 'ai_failure_or_unavailable_reason': ai.get('reason'),
                  'ai_detailed_status': ai['status'], 'fit_call_count': ai['fit_call_count'], 'input_sha256s': [item['sha256'] for item in store.inventory['inputs']],
                  'n_raw': qc['n_raw'], 'n_unique_event_keys': unique, 'n_valid_nonnegative_pet': qc['n_valid_nonnegative_pet'],
                  'n_negative_pet': qc['n_negative_pet'], 'n_invalid': qc['n_invalid'], 'n_ai_input': len(sample),
                  'bootstrap_status': bootstrap_status, 'manual_review_status': 'NOT_PERFORMED', 'independent_accuracy': None,
                  'original_straight_entry_profile_status': 'DEFERRED_OUT_OF_SCOPE',
                  'implemented_features': ['FULL_DATE_BATCHED_PROVIDER_QC', 'FULL_CONDITIONAL_PET_METRICS', 'EVENT_ONLY_CLOCK_SENSITIVITY', 'DATE_CLUSTER_BOOTSTRAP', 'BOUNDED_AI_SAMPLE'],
                  'limitations': ['Recorded interactions are not all traffic exposure', 'Observed date clusters do not establish population generalization', 'No independent PET geometric accuracy'],
                  'not_performed': ['independent PET geometric accuracy', 'paper filter validation'] + (['heldout description'] if ai.get('heldout_metrics') is None else []),
                  'metrics': {key: qc[key] for key in ('n_raw', 'n_valid_nonnegative_pet', 'n_negative_pet', 'n_invalid')},
                  'population_definition': qc['population_definition']}
        for filename, value in [('source_inventory', store.inventory), ('pet_qc', qc), ('pet_result', status), ('pet_analysis', status), ('cluster_validation', ai)]:
            write_json(directory / (filename + '.json'), value)
        canonical = sorted(path.relative_to(directory).as_posix() for path in directory.rglob('*') if path.is_file() and 'figures' not in path.relative_to(directory).parts and path.name not in {'figures_index.json', 'fit_attempts.jsonl'})
        write_json(directory / 'run_manifest.json', {'profile_id': 'PET_INTERACTIONS_V1', 'data_kind': profile.get('data_kind', 'REAL'), 'canonical_files': canonical,
                   'processing_mode': 'DATE_BATCHED_FULL_BASELINE_BOUNDED_AI', 'derived_caches_packaged': False,
                   'local_full_csv_retained': 'Oversized CSV stays local; lossless stable-string Parquet shards carry all rows',
                   'ai_input_population': sample_audit})
        after, _ = source_snapshot(root)
        if after != before:
            raise ValueError('Source changed during bulk run')
        files = {path.relative_to(directory).as_posix(): {'sha256': file_hash(path), 'bytes': path.stat().st_size} for path in directory.rglob('*') if path.is_file()}
        provenance = {'profile_id': 'PET_INTERACTIONS_V1', 'data_kind': profile.get('data_kind', 'REAL'), 'status': status, 'source_snapshot': before,
                      'config_ref': profile_relative, 'config_sha256': config_hash, 'rights_manifest_ref': profile['rights_manifest'],
                      'rights_manifest_sha256': file_hash(child(root, profile['rights_manifest'])), 'rights': rights['rights'],
                      'environment_manifest_ref': env_ref, 'environment_manifest_sha256': file_hash(child(root, env_ref)),
                      'lock_sha256': file_hash(child(root, 'requirements.lock')), 'inputs': store.inventory['inputs'],
                      'input_sha256_list': status['input_sha256s'], 'fit_call_count': ai['fit_call_count'], 'files': files,
                      'elapsed_seconds': time.monotonic() - start, 'command': [sys.executable, 'tools/pet_bulk.py', '--profile', profile_relative, '--output', output] + (['--qc-only'] if qc_only else []),
                      'derived_cache_refs': [input_cache.relative_to(root).as_posix(), metrics_cache.relative_to(root).as_posix()],
                      'raw_archives_packaged': False, 'tracks_parsed': False, 'report_generated': False,
                      'export_omissions': {'full_raw_provider_payload': 'Omitted; typed scalar fields, minimal invalid values and source refs retained', 'derived_sqlite_caches': 'Local rebuildable cache, excluded from run artifacts and handoff'}}
        write_json(directory / 'provenance.json', provenance)
        return directory, status
    except BaseException as error:
        for writer in writers:
            if not writer.csv.closed:
                writer.csv.close()
        failure = directory / 'run_failure.json'
        if not failure.exists():
            write_json(failure, {'status': 'FAILED_OR_INCOMPLETE', 'reason': str(error), 'results_preserved': True})
        raise
    finally:
        if aggregate is not None:
            aggregate.close()
        if store is not None:
            store.close()
