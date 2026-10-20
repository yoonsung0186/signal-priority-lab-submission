"""Synthetic numerical evidence fixtures; no estimator fits or real executions."""
import unittest
import json
import pathlib
import uuid
from unittest.mock import patch
from spl.finish_evidence import select_cases, denominator_rows, reference_signal, assess_frozen_model, audit_cases, decode_row, clock_review, _verified_json, run_evidence
from spl.pet_io import file_hash
from spl.pet_signals import SignalIndex


def event(identifier, pet=2, valid=True, scenario='A'):
    return {'event_key': 'SYNTHETIC/TEST/' + str(identifier), 'event_id': str(identifier), 'scenario_id': scenario,
            'data_kind': 'SYNTHETIC', 'pet_s': pet, 'pet_eligible': valid, 'valid_nonnegative_pet': valid and pet >= 0,
            'qc_status': 'INVALID' if not valid else 'NEGATIVE_PET_REVIEW' if pet < 0 else 'VALID_NONNEGATIVE_PET',
            'event_date_utc': '2025-01-01', 'event_date_local': '2025-01-02', 'n_source_rows': 1,
            'ts_enter_encroaching_ms': 1000, 'ts_leave_encroaching_ms': 2000,
            'ts_enter_priority_ms': 2000 + pet * 1000, 'ts_leave_priority_ms': 5000 + pet * 1000}


class FinishEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_root = pathlib.Path(__file__).resolve().parents[1] / 'worker_evidence' / ('finish_unit_fixtures_' + uuid.uuid4().hex[:12])
        cls.fixture_root.mkdir(parents=True, exist_ok=False)

    def test_selection_all_signed_negative_all_short_and_numeric_ordinary(self):
        rows = [event(10), event(2), event(1, 7), event(3, 8), event(99, -.2), event(100, -1, False), event(200, .5), event(201, .5, False)]
        selected, counts, scope = select_cases(iter(rows))
        reasons = {row['event_id']: row['selection_reasons'] for row in selected}
        self.assertEqual(set(reasons), {'1', '2', '3', '99', '100', '200'})
        self.assertIn('ALL_FINITE_NEGATIVE_PET', reasons['100'])
        self.assertEqual(counts['valid_short_1s'], 1)
        self.assertEqual(scope['utc_calendar_date_count'], 1); self.assertEqual(scope['local_calendar_date_count'], 1)

    def test_selection_ordinary_does_not_filter_adverse_invalid_cases(self):
        rows = [event(1, -2, False), event(2, 9, False), event(3, 200), event(4, .2)]
        selected, _, _ = select_cases(rows)
        self.assertEqual(len(selected), 4)
        self.assertIn('PREDECLARED_NUMERIC_ID_FIRST_3_PER_SCENARIO', selected[0]['selection_reasons'])

    def test_zero_denominators_are_null_and_raw_filter_distinct(self):
        rows = denominator_rows({'unique_ledger_rows': 2, 'INVALID': 2}, {'n_raw': 3, 'finite_negative': 1})
        self.assertEqual(rows[0]['denominator'], 3)
        self.assertIsNone(rows[-1]['fraction']); self.assertEqual(rows[-1]['denominator'], 0)

    def test_signal_halfopen_overlap_unknown_and_invalid_block(self):
        rows = [{'signal_group_id': 'G', 'start_timestamp_ms': '0', 'end_timestamp_ms': '100', 'signal_state': 'red'},
                {'signal_group_id': 'G', 'start_timestamp_ms': '100', 'end_timestamp_ms': '200', 'signal_state': 'green'},
                {'signal_group_id': 'G', 'start_timestamp_ms': '150', 'end_timestamp_ms': '160', 'signal_state': 'INVALID'}]
        index = SignalIndex.from_rows(rows)
        self.assertEqual(reference_signal(index, 'G', 99), 'red')
        self.assertEqual(reference_signal(index, 'G', 100), 'green')
        self.assertEqual(reference_signal(index, 'G', 150), 'SIGNAL_AMBIGUOUS')
        self.assertEqual(reference_signal(index, 'G', 200), 'UNKNOWN')
        index = SignalIndex.from_rows(rows + [{'signal_group_id': 'G', 'start_timestamp_ms': 'bad', 'end_timestamp_ms': '200', 'signal_state': 'red'}])
        self.assertEqual(reference_signal(index, 'G', 100), 'UNKNOWN')

    def test_frozen_model_dimension_dominance_without_fit(self):
        model = {'feature_names': ['encroaching_occupancy_s', 'encroaching_length_m'], 'training_medians': [0, 5],
                 'scaler_center': [0, 5], 'scaler_scale': [1, 1], 'cluster_centers': [[0, 0], [.1, 10]]}
        rows = [dict(event(1), encroaching_occupancy_s=0, encroaching_length_m=5),
                dict(event(2), encroaching_occupancy_s=0, encroaching_length_m=15)]
        assignments = {row['event_key']: {'cluster': label} for row, label in zip(rows, [0, 1])}
        result, tables = assess_frozen_model(rows, assignments, model, {'heldout_metrics': None})
        self.assertEqual(result['additional_fit_calls'], 0)
        self.assertGreater(result['static_dimension_centroid_separation_fraction'], .99)
        self.assertEqual(result['incremental_explanation_status'], 'NOT_ESTABLISHED_BY_DESCRIPTIVE_CLUSTER_COMPARISONS')
        self.assertEqual(tables['ai_frozen_subset_comparison'][0]['agreement_fraction'], 1)
        self.assertIsNone(result['independent_accuracy'])

    def test_frozen_reconstruction_mismatch_rejected_not_refitted(self):
        model = {'feature_names': ['encroaching_length_m'], 'training_medians': [5], 'scaler_center': [5],
                 'scaler_scale': [1], 'cluster_centers': [[0], [10]]}
        row = dict(event(1), encroaching_length_m=5)
        with self.assertRaisesRegex(ValueError, 'RECONSTRUCTION_MISMATCH'):
            assess_frozen_model([row], {row['event_key']: {'cluster': 1}}, model, {})

    def test_tail_flag_does_not_claim_measurement_error(self):
        model = {'feature_names': ['encroaching_length_m'], 'training_medians': [5], 'scaler_center': [5],
                 'scaler_scale': [1], 'cluster_centers': [[0], [10]]}
        rows = [dict(event(i), encroaching_length_m=5 if i < 9 else 15) for i in range(10)]
        assignments = {row['event_key']: {'cluster': int(i == 9)} for i, row in enumerate(rows)}
        result, tables = assess_frozen_model(rows, assignments, model, {})
        self.assertEqual(result['measurement_outlier_confirmation'], 'NOT_PERFORMED')
        self.assertEqual(tables['ai_feature_tail_description'][-1]['n_outside_iqr_fences'], 1)

    def test_typed_csv_nested_boolean_and_timestamps(self):
        row = decode_row({'pet_s': '-.2', 'pet_eligible': 'False', 'source_refs': '[{"source_row_id":1}]', 'event_id': '01', 'ts_enter_priority_ms': '2'})
        self.assertEqual(row['pet_s'], -.2); self.assertFalse(row['pet_eligible'])
        self.assertEqual(row['source_refs'][0]['source_row_id'], 1); self.assertEqual(row['event_id'], '01')

    def test_clock_sensitivity_same_cohort_not_measured_variance(self):
        base = {'scenario_id': 'A', 'clock_offset_s': 0, 'pet_values_unchanged': True, 'signal_clock_shifted': False,
                'max_abs_relative_timestamp_gap_change_s': 0, 'n_valid_nonnegative_pet': 4, 'n_signal_known': 4,
                'n_signal_ambiguous_or_unknown': 0, 'role_state_pair_counts': {'red|green': 4}}
        changed = dict(base, clock_offset_s=.5, role_state_pair_counts={'red|green': 2, 'green|green': 2})
        rows = clock_review([base, changed])
        self.assertEqual(rows[1]['role_pair_distribution_total_variation_from_zero'], .5)
        self.assertIsNone(rows[1]['measured_clock_error_variance'])
        with self.assertRaisesRegex(ValueError, 'COHORT_CHANGED'):
            clock_review([base, dict(changed, n_valid_nonnegative_pet=3)])
        with self.assertRaisesRegex(ValueError, 'INVARIANCE_FAILED'):
            clock_review([base, dict(changed, signal_clock_shifted=True)])

    def test_threshold_cohorts_keep_negative_and_invalid_separate(self):
        _, counts, _ = select_cases([event(1, -.1), event(2, .5), event(3, .8, False), event(4, 3)])
        row = next(row for row in counts['scenario_threshold_counts'] if row['tau_s'] == 1)
        self.assertEqual(row['n_valid_nonnegative_pet'], 2); self.assertEqual(row['n_short_pet'], 1)
        self.assertEqual(row['short_pet_fraction'], .5)

    def test_input_artifact_tamper_rejected_before_analysis(self):
        root = self.fixture_root / uuid.uuid4().hex; run = root / 'runs' / 'input'; run.mkdir(parents=True)
        path = run / 'pet_result.json'; path.write_text('{"data_kind":"SYNTHETIC"}\n', encoding='utf-8')
        provenance = {'files': {'pet_result.json': {'sha256': file_hash(path), 'bytes': path.stat().st_size}}}
        self.assertEqual(_verified_json(root, run, 'pet_result.json', provenance)['data_kind'], 'SYNTHETIC')
        path.write_text('{"data_kind":"REAL"}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
            _verified_json(root, run, 'pet_result.json', provenance)

    def test_collision_and_output_guard_before_source_reads(self):
        root = self.fixture_root / uuid.uuid4().hex
        (root / 'runs' / 'input').mkdir(parents=True); (root / 'runs' / 'existing').mkdir()
        # Mock root ownership only: no native root safety is claimed by this fixture.
        with patch('spl.finish_evidence.verify_root', return_value=root):
            with self.assertRaises(FileExistsError):
                run_evidence(root, 'runs/input', 'runs/existing')
            with self.assertRaisesRegex(ValueError, 'OUTPUT_MUST_BE_NEW_RUN'):
                run_evidence(root, 'runs/input', 'worker_evidence/output')
            with self.assertRaisesRegex(ValueError, 'UNSAFE_RELATIVE_PATH'):
                run_evidence(root, 'runs/input', '../escape')


if __name__ == '__main__':
    unittest.main()
