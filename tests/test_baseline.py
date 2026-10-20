"""Substantive synthetic/contract regressions; no real accuracy claims."""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from spl.cli import run
from spl.contracts import ContractError
from spl.engine import EngineConfig, aggregate, analyze, classify_signal, sensitivity
from spl.safety import child, verify_root
from spl.synthetic import EXPECTED_COUNTS, EXPECTED_LABELS, synthetic_fixture, verify_oracle


def only(*names):
    data = synthetic_fixture()
    data['trajectories'] = [row for row in data['trajectories'] if row['track_id'] in names]
    return data


def event(result, track):
    return next(row for row in result['events'] if row['track_id'] == track)


class TestKnownOracle(unittest.TestCase):
    def test_independent_fixed_labels_counts_and_times(self):
        result = analyze(synthetic_fixture())
        self.assertEqual({row['track_id']: row['classification'] for row in result['events']}, EXPECTED_LABELS)
        self.assertEqual({key: result['cells'][0][key] for key in EXPECTED_COUNTS}, EXPECTED_COUNTS)
        self.assertEqual(verify_oracle(result)['status'], 'PASS')
        self.assertIsNone(result['accuracy'])
        self.assertEqual(result['manual_review_status'], 'NOT_PERFORMED')

    def test_e04_entry_before_red_remains_nonred(self):
        row = event(analyze(only('pre_red_remains')), 'pre_red_remains')
        self.assertEqual(row['classification'], 'NON_RED_CONSISTENT')
        self.assertAlmostEqual(row['t_cross_trajectory_s'], 9.1)

    def test_e05_left_censored(self):
        row = event(analyze(only('already_inside')), 'already_inside')
        self.assertEqual(row['excluded_reason'], 'LEFT_CENSORED')

    def test_e06_reverse_not_entry(self):
        data = only('red')
        data['trajectories'][0]['x_m'] = 0
        data['trajectories'][1]['x_m'] = -2
        self.assertEqual(analyze(data)['cells'][0]['N'], 0)

    def test_e07_finite_segment(self):
        row = event(analyze(only('outside_segment')), 'outside_segment')
        self.assertEqual(row['excluded_reason'], 'OUTSIDE_FINITE_SEGMENT')

    def test_e08_jitter_deduplicated(self):
        result = analyze(only('jitter'))
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['cells'][0]['N'], 1)
        self.assertEqual(result['cells'][0]['C'], 1)

    def test_e09_long_gap_excluded(self):
        self.assertEqual(event(analyze(only('large_gap')), 'large_gap')['excluded_reason'], 'CROSSING_GAP_EXCEEDED')

    def test_e10_stationary_no_zero_division(self):
        self.assertEqual(analyze(only('stationary'))['cells'][0]['N'], 0)

    def test_e11_front_centroid_distinct_profile(self):
        data = only('green')
        data['trajectories'][1]['x_m'] = 2
        front = analyze(data)['events'][0]
        data['boundaries'][0]['crossing_reference'] = 'CENTROID'
        data['boundaries'][0]['profile_version'] = 'centroid-exploratory-1'
        center = analyze(data)['events'][0]
        self.assertAlmostEqual(front['t_cross_trajectory_s'], 5.05)
        self.assertAlmostEqual(center['t_cross_trajectory_s'], 5.1)
        self.assertNotEqual(front['event_key'], center['event_key'])

    def test_e12_missing_body_axis_no_heading_guess(self):
        data = only('green')
        for row in data['trajectories']:
            del row['yaw_rad']
            row['heading_rad'] = 0
        self.assertEqual(analyze(data)['events'][0]['excluded_reason'], 'POSITION_OR_FRONT_GEOMETRY_UNVERIFIED')

    def test_e13_turn_excluded(self):
        self.assertEqual(analyze(only('turn'))['events'][0]['excluded_reason'], 'NON_STRAIGHT_OR_UNKNOWN_MOVEMENT')

    def test_e14_unverified_mapping_excluded(self):
        data = only('green')
        data['boundaries'][0]['mapping_status'] = 'UNVERIFIED'
        self.assertEqual(analyze(data)['events'][0]['excluded_reason'], 'MOVEMENT_SIGNAL_MAPPING_UNVERIFIED')


class TestSignalAndClocks(unittest.TestCase):
    def test_e15_half_open_exact_transition(self):
        signals = synthetic_fixture()['signals']
        self.assertEqual(classify_signal(10, 10, signals), ('RED_CONSISTENT', ['RED']))
        self.assertEqual(classify_signal(20, 20, signals), ('NON_RED_CONSISTENT', ['YELLOW']))
        self.assertEqual(classify_signal(30, 30, signals), ('SIGNAL_AMBIGUOUS', ['UNKNOWN']))

    def test_e03_explicit_transition_uncertainty(self):
        signals = synthetic_fixture()['signals']
        self.assertEqual(classify_signal(9.85, 9.95, signals)[0], 'NON_RED_CONSISTENT')
        for row in signals:
            row['transition_uncertainty_s'] = 0.1
        self.assertEqual(classify_signal(9.85, 9.95, signals)[0], 'SIGNAL_AMBIGUOUS')

    def test_e16_observed_unknown_is_not_nonred(self):
        result = analyze(only('unknown'))
        self.assertEqual(result['cells'][0]['U'], 1)
        self.assertEqual(result['cells'][0]['G'], 0)

    def test_d06_before_first_observed_state_is_unknown(self):
        data = only('green')
        data['signals'] = data['signals'][1:]
        self.assertEqual(analyze(data)['events'][0]['classification'], 'SIGNAL_AMBIGUOUS')

    def test_d08_planned_signal_excluded(self):
        data = only('red')
        for row in data['signals']:
            row['state_basis'] = 'PLAN'
        self.assertEqual(analyze(data)['events'][0]['excluded_reason'], 'PLAN_SIGNAL_NOT_OBSERVATION')

    def crossing_near_red(self):
        data = only('green')
        data['trajectories'][0]['timestamp_s'] = 9.85
        data['trajectories'][1]['timestamp_s'] = 9.95
        return data

    def test_e17_offset_moves_only_vehicle_clock(self):
        data = self.crossing_near_red()
        preserved = deepcopy(data)
        nominal = analyze(data)
        shifted = analyze(data, EngineConfig(clock_offset_s=0.2))
        self.assertEqual(nominal['events'][0]['classification'], 'NON_RED_CONSISTENT')
        self.assertEqual(shifted['events'][0]['classification'], 'RED_CONSISTENT')
        self.assertAlmostEqual(shifted['events'][0]['t_cross_aligned_s'], 10.1)
        self.assertEqual(data, preserved)
        self.assertEqual(data['signals'][1]['t_start_s'], 10)

    def test_e18_both_clock_origin_translation_invariant(self):
        data = synthetic_fixture()
        original = analyze(data)
        for row in data['records']:
            row['t_start_s'] += 100; row['t_end_s'] += 100
        for row in data['trajectories']:
            row['timestamp_s'] += 100
        for table in ('signals', 'observation_intervals'):
            for row in data[table]:
                row['t_start_s'] += 100; row['t_end_s'] += 100
        for row in data['boundaries']:
            row['validity_interval'] = [time+100 for time in row['validity_interval']]
        shifted = analyze(data)
        self.assertEqual([e['classification'] for e in original['events']], [e['classification'] for e in shifted['events']])
        for before, after in zip(original['events'], shifted['events']):
            if before['classification'] != 'EXCLUDED':
                self.assertAlmostEqual(after['t_cross_trajectory_s'], before['t_cross_trajectory_s']+100)

    def test_e19_offset_and_quantization_not_double_counted(self):
        data = self.crossing_near_red()
        data['records'][0]['timestamp_uncertainty_s'] = 0.02
        row = analyze(data, EngineConfig(clock_offset_s=0.2, clock_uncertainty_s=0.03))['events'][0]
        self.assertAlmostEqual(row['t_lo_aligned_s'], 10.0)
        self.assertAlmostEqual(row['t_hi_aligned_s'], 10.2)
        self.assertEqual(row['classification'], 'RED_CONSISTENT')
        self.assertFalse(row['uncertainty_components']['source_resolution_used_as_additional_error'])


class TestRatesExposureCohorts(unittest.TestCase):
    def test_m01_m02_denominator_and_mutually_exclusive_exclusions(self):
        cell = analyze(synthetic_fixture())['cells'][0]
        self.assertEqual(cell['N'], cell['C']+cell['G']+cell['U'])
        self.assertEqual(cell['excluded_count'], sum(cell['excluded_reason_counts'].values()))
        self.assertEqual(cell['N']+cell['excluded_count'], cell['inspected_track_boundary_count'])

    def test_m03_zero_denom_is_null(self):
        cell = analyze(only('stationary'))['cells'][0]
        self.assertEqual(cell['N'], 0)
        for field in ('rate_lower_per_1000', 'rate_upper_per_1000', 'ambiguous_fraction'):
            self.assertIsNone(cell[field])

    def test_m04_zero_observed_red_is_preserved(self):
        cell = analyze(only('green'))['cells'][0]
        self.assertEqual(cell['C'], 0)
        self.assertEqual(cell['rate_lower_per_1000'], 0)
        self.assertEqual(cell['priority_tag'], 'DESCRIPTIVE_ONLY')

    def test_m05_all_ambiguous_not_safe(self):
        cell = analyze(only('transition', 'unknown'))['cells'][0]
        self.assertEqual((cell['rate_lower_per_1000'], cell['rate_upper_per_1000']), (0, 1000))
        self.assertIsNone(cell['population_ci_95'])
        self.assertEqual(cell['uncertainty_range_kind'], 'CLASSIFICATION_BOUNDS_NOT_95_PERCENT_CI')

    def test_m07_union_not_vehicle_seconds(self):
        cell = analyze(synthetic_fixture())['cells'][0]
        self.assertEqual(cell['valid_observation_seconds'], 25)
        self.assertEqual(cell['red_observation_seconds'], 10)
        self.assertEqual(cell['count_per_hour'], 288)

    def test_missing_exposure_null(self):
        data = only('red')
        data['observation_intervals'] = []
        cell = analyze(data)['cells'][0]
        self.assertIsNone(cell['valid_observation_seconds'])
        self.assertIsNone(cell['count_per_hour'])

    def test_partial_exposure_full_record_numerator_never_divided(self):
        data = only('red')
        data['observation_intervals'] = [dict(data['observation_intervals'][0], t_start_s=0, t_end_s=10)]
        cell = analyze(data)['cells'][0]
        self.assertEqual(cell['C'], 1)
        self.assertEqual(cell['valid_observation_seconds'], 10)
        self.assertEqual(cell['exposure_red_count'], 0)
        self.assertEqual(cell['exposure_uncovered_red_count'], 1)
        self.assertIsNone(cell['count_per_hour'])
        self.assertEqual(cell['exposure_status'], 'NUMERATOR_OUTSIDE_EXPOSURE_COUNT_PER_HOUR_UNAVAILABLE')

    def test_exposure_intersects_boundary_validity(self):
        data = only('red')
        data['boundaries'][0]['validity_interval'] = [14, 16]
        cell = analyze(data)['cells'][0]
        self.assertEqual(cell['valid_observation_seconds'], 2)
        self.assertEqual(cell['red_observation_seconds'], 2)
        self.assertEqual(cell['exposure_red_count'], 1)
        self.assertEqual(cell['count_per_hour'], 1800)

    def test_unverified_definition_never_exports_zero_intensity(self):
        for cause in ('mapping', 'units', 'coordinates', 'time_alignment', 'plan'):
            data = only('red')
            if cause == 'mapping':
                data['boundaries'][0]['mapping_status'] = 'UNVERIFIED'
            elif cause == 'units':
                data['records'][0]['unit_basis'] = 'UNKNOWN'
            elif cause == 'coordinates':
                data['boundaries'][0]['coordinate_frame'] = 'OTHER_FRAME'
            elif cause == 'time_alignment':
                data['records'][0]['time_alignment_status'] = 'UNKNOWN'
            elif cause == 'plan':
                data['signals'][0]['state_basis'] = 'PLAN'
            with self.subTest(cause=cause):
                cell = analyze(data)['cells'][0]
                self.assertEqual(cell['N'], 0)
                self.assertIsNone(cell['valid_observation_seconds'])
                self.assertIsNone(cell['red_observation_seconds'])
                self.assertIsNone(cell['count_per_hour'])
                self.assertEqual(cell['exposure_status'], 'UNVERIFIED_ANALYSIS_DEFINITION')
                self.assertIsNotNone(cell['exposure_blocked_reason'])

    def test_m09_scenario_population_and_common_cohort(self):
        data = only('red', 'green')
        for row in data['trajectories']:
            if row['track_id'] == 'green':
                row['x_m'] = -1.4 if row['timestamp_s'] == 5 else -0.6
        result = sensitivity(data, clock_offsets=[0], boundary_offsets=[-0.5, 0, 0.5])
        self.assertEqual([row['valid_event_count'] for row in result['scenarios']], [1, 2, 1])
        self.assertEqual([row['common_event_count'] for row in result['scenarios']], [1, 1, 1])
        self.assertEqual([row['common_cohort_cells'][0]['N'] for row in result['scenarios']], [1, 1, 1])
        self.assertFalse(result['signal_clock_shifted'])

    def test_grid_fifteen_scenarios(self):
        self.assertEqual(len(sensitivity(only('red'))['scenarios']), 15)


class TestNormalizedContract(unittest.TestCase):
    def test_d02_record_composite_track_keys(self):
        data = only('green')
        second = deepcopy(data['records'][0]); second['record_id'] = 'record-B'
        data['records'].append(second)
        for table in ('trajectories', 'signals'):
            second_rows = deepcopy(data[table])
            for row in second_rows:
                row['record_id'] = 'record-B'
            data[table].extend(second_rows)
        result = analyze(data)
        self.assertEqual(len(result['events']), 2)
        self.assertNotEqual(result['events'][0]['event_key'], result['events'][1]['event_key'])
        self.assertEqual(sum(cell['N'] for cell in result['cells']), 2)

    def test_d03_time_order_and_duplicate_track_exclusion(self):
        for duplicate in (False, True):
            data = only('green')
            if duplicate:
                data['trajectories'].append(deepcopy(data['trajectories'][1]))
            else:
                data['trajectories'].reverse()
            self.assertEqual(analyze(data)['events'][0]['excluded_reason'], 'TRACK_TIME_ORDER_OR_DUPLICATE_CONFLICT')

    def test_d05_invalid_signal_code_rejected(self):
        data = only('green'); data['signals'][0]['state'] = '0'
        with self.assertRaises(ContractError): analyze(data)

    def test_overlapping_signal_fails_closed(self):
        data = only('green'); data['signals'][0]['t_end_s'] = 11
        with self.assertRaisesRegex(ContractError, 'OVERLAPPING_SIGNAL_INTERVALS'): analyze(data)

    def test_d07_last_signal_not_extended(self):
        data = only('unknown'); data['signals'][-1]['t_end_s'] = 26
        self.assertEqual(analyze(data)['events'][0]['classification'], 'SIGNAL_AMBIGUOUS')

    def test_d11_unknown_units_excluded(self):
        data = only('red'); data['records'][0]['unit_basis'] = 'UNKNOWN'
        self.assertEqual(analyze(data)['events'][0]['excluded_reason'], 'COORDINATE_OR_TIME_UNITS_UNVERIFIED')

    def test_d14_mixed_real_synthetic_rejected(self):
        data = only('red')
        record = deepcopy(data['records'][0]); record['record_id'] = 'real-record'; record['data_kind'] = 'REAL'
        data['records'].append(record)
        with self.assertRaisesRegex(ContractError, 'REAL_SYNTHETIC_MIX_FORBIDDEN'): analyze(data)

    def test_unknown_real_rights_blocked(self):
        data = only('red'); data['records'][0]['data_kind'] = 'REAL'
        data.pop('fixture_kind')
        data['records'][0]['source_refs'] = ['OFFICIAL_SOURCE_REFERENCE']
        with self.assertRaisesRegex(ContractError, 'REAL_ANALYSIS_RIGHTS_NOT_ALLOW'): analyze(data)

    def fake_real_with_rights(self):
        data = only('red')
        data['records'][0]['data_kind'] = 'REAL'
        dataset = data['records'][0]['dataset_id']
        data['rights'] = {dataset: {'access_status': 'ALLOW', 'analysis_status': 'ALLOW', 'license_evidence': ['CLAIMED_ALLOW']}}
        return data

    def test_o10_synthetic_fixture_cannot_be_relabelled_real(self):
        data = self.fake_real_with_rights()
        with self.assertRaisesRegex(ContractError, 'REAL_RECORD_WITH_SYNTHETIC_FIXTURE_TAG'): analyze(data)

    def test_o10_real_cannot_use_synthetic_source_reference(self):
        data = self.fake_real_with_rights()
        data.pop('fixture_kind')
        with self.assertRaisesRegex(ContractError, 'REAL_RECORD_WITH_SYNTHETIC_SOURCE_REFERENCE'): analyze(data)

    def test_o10_real_cannot_use_synthetic_movement_truth(self):
        data = self.fake_real_with_rights()
        data.pop('fixture_kind')
        data['records'][0]['source_refs'] = ['OFFICIAL_SOURCE_REFERENCE']
        with self.assertRaisesRegex(ContractError, 'REAL_TRAJECTORY_WITH_SYNTHETIC_TRUTH_BASIS'): analyze(data)

    def test_d15_source_row_trace(self):
        row = analyze(only('red'))['events'][0]
        self.assertEqual(row['source_row_ids'], ['red:0', 'red:1'])

    def test_nonfinite_bool_and_string_numbers_rejected(self):
        for value in (math.nan, math.inf, -math.inf, '1.0', True):
            data = only('red'); data['trajectories'][0]['x_m'] = value
            with self.subTest(value=repr(value)), self.assertRaises(ContractError): analyze(data)

    def test_unknown_boundary_profile_explicitly_excluded(self):
        data = only('red'); data['boundaries'] = []
        self.assertEqual(analyze(data)['events'][0]['excluded_reason'], 'NO_VERIFIED_BOUNDARY_PROFILE')


class TestLocalOutputSafety(unittest.TestCase):
    def test_s03_s04_path_traversal_drive_unc(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ('../other', '/tmp/other', 'C:/other', '\\\\host\\share', 'runs/../../other'):
            with self.subTest(relative=relative), self.assertRaises(ValueError): child(root, relative)

    def test_s01_root_marker_required(self):
        with self.assertRaises(ValueError): verify_root(Path(__file__).parent)

    def test_s05_mocked_reparse_rejected(self):
        root = Path(__file__).resolve().parents[1]
        with patch('spl.safety.linked', side_effect=lambda path: path == root/'runs'):
            with self.assertRaisesRegex(ValueError, 'CHILD_LINK_OR_REPARSE'): child(root, 'runs/new')

    def test_s09_completed_output_not_overwritten(self):
        root = Path(__file__).resolve().parents[1]
        output = 'runs/test_baseline_preserved_'+uuid.uuid4().hex[:12]
        directory, result = run(only('red'), output, root, input_ref='UNIT_TEST_SYNTHETIC', include_sensitivity=False)
        original = (directory/'result.json').read_bytes()
        with self.assertRaises(FileExistsError): run(only('red'), output, root, input_ref='UNIT_TEST_SYNTHETIC', include_sensitivity=False)
        self.assertEqual((directory/'result.json').read_bytes(), original)
        self.assertEqual(result['data_kind'], 'SYNTHETIC')


if __name__ == '__main__':
    unittest.main()
