"""Synthetic full/batched agreement tests; actual estimator fitting forbidden."""
from copy import deepcopy
import csv
import json
import pathlib
import unittest
import uuid
from unittest.mock import patch
from spl.pet_bulk import AggregateStore, StreamingTable, export_event, run_bulk
from spl.pet_metrics import summarize, bootstrap_descriptions


def rows(n=84):
    output = []
    for index in range(n):
        value = (-.2 if index == 0 else (index % 20) / 10)
        output.append({'event_key': 'SYNTHETIC/TEST/' + str(index), 'event_id': str(index), 'data_kind': 'SYNTHETIC',
                       'scenario_id': 'A' if index % 2 else 'B', 'pet_s': value, 'pet_eligible': True,
                       'valid_nonnegative_pet': value >= 0, 'qc_status': 'NEGATIVE_PET_REVIEW' if value < 0 else 'VALID_NONNEGATIVE_PET',
                       'n_source_rows': 1, 'event_date_local': '2025-01-%02d' % (1 + index % 7), 'event_date_utc': '2025-01-%02d' % (1 + index % 7),
                       'local_4h_bin': (index % 6) * 4, 'encroaching_object_id': str(index), 'priority_object_id': str(1000 + index),
                       'encroaching_class': 'vehicle', 'priority_class': 'pedestrian', 'encroaching_occupancy_s': .1 + index / 20,
                       'priority_occupancy_s': .5, 'encroaching_length_m': 4 + index / 100, 'encroaching_width_m': 2,
                       'priority_length_m': 1, 'priority_width_m': .5, 'invalid_reasons': [], 'review_reasons': []})
    return output


def context(events):
    return [{'event_key': row['event_key'], 'role': role, 'signal_state': 'UNKNOWN' if int(row['event_id']) % 3 == 0 else 'red'} for row in events for role in ('encroaching', 'priority')]


class PETBulkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = pathlib.Path(__file__).resolve().parents[1] / 'worker_evidence' / ('pet_bulk_unit_fixtures_' + uuid.uuid4().hex[:12])
        cls.base.mkdir(parents=True, exist_ok=False)

    def store(self, events, split=10):
        aggregate = AggregateStore(self.base / (uuid.uuid4().hex + '.sqlite'))
        self.addCleanup(aggregate.close)
        for start in range(0, len(events), split):
            chunk = events[start:start + split]
            aggregate.add(chunk, context(chunk), [])
        aggregate.finish()
        return aggregate

    def test_full_metrics_equal_existing_eager_oracle_across_batches(self):
        events = rows(); aggregate = self.store(events)
        expected = summarize(events, context(events), {'A': {}, 'B': {}, 'EMPTY': {}})
        actual = aggregate.metrics({'A': {}, 'B': {}, 'EMPTY': {}})
        for key in ('scenario_summary', 'pet_thresholds', 'timebin_summary'):
            self.assertEqual(actual[key], expected[key], key)
        self.assertEqual(list(aggregate.ecdf_rows()), expected['pet_ecdf'])
        empty = [row for row in actual['pet_thresholds'] if row['scenario_id'] == 'EMPTY']
        self.assertTrue(all(row['short_pet_fraction'] is None for row in empty))

    def test_bootstrap_equal_existing_exact_date_cluster_oracle(self):
        events = rows(); aggregate = self.store(events)
        profile = {'scope': 'MULTIDAY_CASE', 'thresholds_s': [.5, 1, 1.5, 2], 'bootstrap': {'min_days': 7, 'min_events': 30, 'repetitions': 200, 'seed': 20261012}}
        eager = summarize(events, context(events), {'A': {}, 'B': {}})
        expected, _ = bootstrap_descriptions(events, eager, profile)
        actual, status = aggregate.bootstrap(aggregate.metrics({'A': {}, 'B': {}}), profile)
        self.assertEqual(actual, expected); self.assertEqual(status['repetitions'], 200)

    def test_sample_day_calendar_dates_do_not_enable_bootstrap(self):
        aggregate = self.store(rows())
        result, status = aggregate.bootstrap(aggregate.metrics({'A': {}, 'B': {}}), {'scope': 'SAMPLE_DAY'})
        self.assertEqual(result, []); self.assertEqual(status['status'], 'NOT_APPLICABLE')

    def test_ai_sample_bounded_stratified_deterministic_and_no_pet_selection(self):
        events = rows(); first = self.store(events, 7); second = self.store(list(reversed(events)), 13)
        a, audit = first.ai_sample(25); b, other = second.ai_sample(25)
        self.assertEqual([row['event_key'] for row in a], [row['event_key'] for row in b])
        self.assertEqual(audit, other); self.assertEqual(len(a), 25)
        self.assertFalse(audit['selection_used_pet_or_signal_values'])
        changed = deepcopy(events)
        for row in changed:
            row['pet_s'] = 500
        c, _ = self.store(changed).ai_sample(25)
        self.assertEqual([row['event_key'] for row in a], [row['event_key'] for row in c])

    def test_conflicting_and_invalid_rows_never_enter_ai_but_negative_retained(self):
        events = rows(8); events[1]['qc_status'] = 'INVALID'; events[1]['pet_eligible'] = False
        selected, audit = self.store(events).ai_sample(100)
        self.assertEqual(len(selected), 7); self.assertIn('SYNTHETIC/TEST/0', {row['event_key'] for row in selected})
        self.assertNotIn('SYNTHETIC/TEST/1', {row['event_key'] for row in selected})

    def test_ecdf_display_bounded_and_endpoint_is_one(self):
        events = rows(1000)
        for index, row in enumerate(events):
            row.update(pet_s=index / 100, valid_nonnegative_pet=True, qc_status='VALID_NONNEGATIVE_PET')
        aggregate = self.store(events)
        display = aggregate.ecdf_display()
        for scenario in ('A', 'B'):
            cohort = [row for row in display if row['scenario_id'] == scenario]
            self.assertLessEqual(len(cohort), 256); self.assertEqual(cohort[-1]['ecdf'], 1)

    def test_streaming_shards_preserve_rows_schema_and_exclusive_collision(self):
        import pyarrow.parquet as pq
        directory = self.base / uuid.uuid4().hex; directory.mkdir()
        writer = StreamingTable(directory, 'SYNTHETIC', 2)
        writer.add([{'id': 1, 'value': None}, {'id': 2, 'value': 1.25}, {'id': 3, 'value': 2}]); writer.close()
        index = json.loads((directory / 'SYNTHETIC_parquet_index.json').read_text())
        self.assertEqual(index['row_count'], 3); self.assertEqual([row['row_count'] for row in index['shards']], [2, 1])
        values = [row for shard in index['shards'] for row in pq.read_table(directory / shard['path']).to_pylist()]
        self.assertEqual(values, [{'id': '1', 'value': ''}, {'id': '2', 'value': '1.25'}, {'id': '3', 'value': '2'}])
        with self.assertRaises(FileExistsError):
            StreamingTable(directory, 'SYNTHETIC')

    def test_invalid_keys_use_source_identity_and_raw_payload_not_exported(self):
        template = {'event_key': 'SYNTHETIC/TEST/INVALID_KEY_0', 'dataset_id': 'SYNTHETIC', 'source_version': 'TEST', 'event_id': None,
                    'provider_fields': {'event_id': 'bad'}, 'conflicting_source_variants': [], 'source_refs': [{'source_index': 0, 'source_row_id': 1}]}
        a = export_event(template); b = export_event(dict(template, source_refs=[{'source_index': 0, 'source_row_id': 2}]))
        self.assertNotEqual(a['event_key'], b['event_key']); self.assertNotIn('provider_fields', a)
        self.assertEqual(a['invalid_original_values']['event_id'], 'bad')

    def test_unknown_signals_preserve_full_pet_denom_and_null_subset_fraction(self):
        events = rows(10); aggregate = AggregateStore(self.base / (uuid.uuid4().hex + '.sqlite'))
        self.addCleanup(aggregate.close)
        unknown = [dict(row, signal_state='UNKNOWN') for row in context(events)]
        aggregate.add(events, unknown, []); aggregate.finish()
        result = aggregate.metrics({'A': {}, 'B': {}})
        self.assertEqual(sum(row['n_valid_nonnegative_pet'] for row in result['scenario_summary']), 9)
        self.assertTrue(all(row['n_signal_known'] == 0 and row['short_pet_fraction_signal_known'] is None for row in result['pet_thresholds']))

    def test_sensitivity_aggregates_same_clock_cohort_without_normalizing_unknown(self):
        aggregate = AggregateStore(self.base / (uuid.uuid4().hex + '.sqlite'))
        self.addCleanup(aggregate.close)
        first = {'scenario_id': 'A', 'clock_offset_s': .5, 'n_valid_nonnegative_pet': 2, 'n_signal_known': 1,
                 'n_signal_ambiguous_or_unknown': 1, 'pet_values_unchanged': True, 'max_abs_relative_timestamp_gap_change_s': 0,
                 'signal_clock_shifted': False, 'basis': 'SYNTHETIC_OFFSET', 'role_state_pair_counts': {'red|green': 1, 'UNKNOWN|green': 1}}
        aggregate.add([], [], [first]); aggregate.add([], [], [dict(first, n_signal_known=0, n_signal_ambiguous_or_unknown=2, role_state_pair_counts={'UNKNOWN|green': 2})])
        result = aggregate.timing['A', .5]
        self.assertEqual(result['n_valid_nonnegative_pet'], 4); self.assertEqual(result['n_signal_known'], 1)
        self.assertEqual(result['role_state_pair_counts']['UNKNOWN|green'], 3)

    def test_mocked_synthetic_complete_qc_run_contract_and_no_estimator_calls(self):
        from test_pet import fixture, signals, PROFILE
        from spl.pet_qc import build_ledger
        from spl.safety import child
        root = self.base / uuid.uuid4().hex; root.mkdir(); (root / 'runs').mkdir()
        (root / 'env.json').write_text('{}', encoding='utf-8'); (root / 'rights.json').write_text('{}', encoding='utf-8')
        (root / 'requirements.lock').write_text('SYNTHETIC_FIXTURE', encoding='utf-8')
        data = fixture(); inventory_rows = []
        for index, (table, values) in enumerate(data.items()):
            inventory_rows.append({'table': table, 'source_file_sha256': 'SYNTHETIC_FIXTURE_HASH', 'row_count': len(values)})
            for number, value in enumerate(values, 1):
                value['__source'] = {'source_index': index, 'source_row_id': number}
        _, expected, _, _ = build_ledger(data, PROFILE, {'tables': inventory_rows})
        class MockStore:
            inventory = {'inputs': [{'archive': 'SYNTHETIC_NOT_REAL.zip', 'sha256': 'SYNTHETIC_FIXTURE_HASH', 'bytes': 0}], 'tables': inventory_rows}
            global_qc = {'event_rows': expected['event_rows'], 'object_rows': expected['object_rows'], 'signal_rows': {'n_raw': 2}}
            closed = False
            def batches(self):
                yield data, signals()
            def close(self):
                self.closed = True
        store = MockStore()
        def reserve(_, relative):
            path = child(root, relative); path.mkdir(exist_ok=False); return path
        with patch('spl.pet_bulk.verify_root', return_value=root), patch('spl.pet_bulk.new_run', side_effect=reserve), \
             patch('spl.pet_bulk.load_profile', return_value=(dict(PROFILE, scope='SAMPLE_DAY', rights_manifest='rights.json'), {'rights': {}}, 'SYNTHETIC_CONFIG_HASH')), \
             patch('spl.pet_bulk.source_snapshot', return_value=({'files': [], 'sha256': 'SYNTHETIC_MOCKED_SOURCE_HASH'}, 'env.json')), \
             patch('spl.pet_batch_io.prepare', return_value=store), patch('spl.pet_bulk.analyze_types', side_effect=AssertionError('No estimator in this test')), \
             patch('spl.pet_bulk.figures'):
            directory, result = run_bulk(root, 'SYNTHETIC_PROFILE.yaml', 'runs/SYNTHETIC_QC', True)
        self.assertEqual(result['data_kind'], 'SYNTHETIC'); self.assertEqual(result['n_raw'], 1)
        self.assertEqual(result['n_valid_nonnegative_pet'], 1); self.assertEqual(result['fit_call_count'], 0)
        provenance = json.loads((directory / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(provenance['status'], result); self.assertEqual(provenance['config_sha256'], 'SYNTHETIC_CONFIG_HASH')
        self.assertEqual(provenance['fit_call_count'], 0); self.assertTrue(store.closed)
        manifest = json.loads((directory / 'run_manifest.json').read_text(encoding='utf-8'))
        self.assertIn('event_ledger.csv', manifest['canonical_files']); self.assertNotIn('provenance.json', manifest['canonical_files'])


if __name__ == '__main__':
    unittest.main()
