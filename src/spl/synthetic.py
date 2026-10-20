"""Explicit synthetic fixture and manually specified deterministic oracle."""
from __future__ import annotations


EXPECTED_LABELS = {
    'green': 'NON_RED_CONSISTENT', 'red': 'RED_CONSISTENT', 'transition': 'SIGNAL_AMBIGUOUS',
    'unknown': 'SIGNAL_AMBIGUOUS', 'jitter': 'RED_CONSISTENT', 'pre_red_remains': 'NON_RED_CONSISTENT',
    'already_inside': 'EXCLUDED', 'turn': 'EXCLUDED', 'large_gap': 'EXCLUDED',
    'stationary': 'EXCLUDED', 'outside_segment': 'EXCLUDED',
}
EXPECTED_COUNTS = {'N': 6, 'C': 2, 'G': 2, 'U': 2, 'excluded_count': 5}
EXPECTED_CROSSING_TIMES = {'green': 5.1, 'red': 15.1, 'transition': 10.0, 'unknown': 27.1,
                           'jitter': 16.316666666666666, 'pre_red_remains': 9.1}


def synthetic_fixture():
    record = {'dataset_id': 'synthetic-known-oracle', 'record_id': 'record-A', 'source_version': 'fixture-v1',
              'site_id': 'SYNTHETIC_SITE', 'data_kind': 'SYNTHETIC', 't_start_s': 0, 't_end_s': 30,
              'time_basis': 'RECORD_RELATIVE', 'coordinate_frame': 'SYNTHETIC_LOCAL_METERS',
              'unit_basis': 'METERS_SECONDS', 'source_refs': ['src/spl/synthetic.py'],
              'time_alignment_status': 'ASSUMPTION_BASED', 'timestamp_uncertainty_s': 0}
    boundary = {'dataset_id': record['dataset_id'], 'site_id': record['site_id'], 'approach_id': 'eastbound',
                'boundary_id': 'synthetic-x0', 'profile_version': '1', 'movement': 'STRAIGHT', 'signal_group_id': 'S1',
                'directed_line': [[0, -5], [0, 5]], 'inward_normal': [1, 0], 'line_kind': 'ANALYSIS_BOUNDARY',
                'crossing_reference': 'FRONT_BUMPER', 'coordinate_frame': record['coordinate_frame'],
                'validity_interval': [0, 31], 'mapping_status': 'VERIFIED', 'evidence_refs': ['SYNTHETIC_KNOWN_GEOMETRY']}
    signals = []
    for index, (start, end, state) in enumerate([(0, 10, 'GREEN'), (10, 20, 'RED'), (20, 25, 'YELLOW'), (25, 30, 'UNKNOWN')]):
        signals.append({'dataset_id': record['dataset_id'], 'record_id': record['record_id'], 'signal_group_id': 'S1',
                        't_start_s': start, 't_end_s': end, 'state': state, 'state_basis': 'OBSERVED',
                        'source_resolution_s': 0.1, 'transition_uncertainty_s': 0, 'source_row_ids': ['synthetic-signal-%d' % index]})
    tracks = []
    def track(name, samples, y=0, movement='STRAIGHT'):
        for index, (timestamp, front_x) in enumerate(samples):
            tracks.append({'dataset_id': record['dataset_id'], 'record_id': record['record_id'], 'track_id': name,
                           'site_id': record['site_id'], 'class': 'CAR', 'x_m': front_x-1, 'y_m': y,
                           'timestamp_s': timestamp, 'position_reference': 'CENTROID', 'source_row_id': name+':%d' % index,
                           'approach_id': 'eastbound', 'movement': movement, 'movement_basis': 'SYNTHETIC_TRUTH',
                           'length_m': 2, 'yaw_rad': 0, 'body_direction_basis': 'DOCUMENTED_FORWARD_AXIS'})
    track('green', [(5, -1), (5.2, 1)])
    track('red', [(15, -1), (15.2, 1)])
    track('transition', [(9.9, -1), (10.1, 1)])
    track('unknown', [(27, -1), (27.2, 1)])
    track('already_inside', [(15, 1), (15.2, 2)])
    track('turn', [(15, -1), (15.2, 1)], movement='LEFT')
    track('large_gap', [(15, -1), (16, 1)])
    track('stationary', [(7, -1), (7.2, -1)])
    track('jitter', [(16, -1), (16.1, -0.1), (16.2, 0.1), (16.3, -0.05), (16.4, 0.25), (16.5, -0.2), (16.6, 0.4)])
    track('outside_segment', [(15, -1), (15.2, 1)], y=8)
    track('pre_red_remains', [(9, -1), (9.2, 1), (15, 2)])
    observations = [{'dataset_id': record['dataset_id'], 'record_id': record['record_id'], 'approach_id': 'eastbound',
                     't_start_s': start, 't_end_s': end, 'status': 'VERIFIED'} for start, end in [(0, 12), (8, 26)]]
    return {'schema_version': 'spl-normalized-1', 'records': [record], 'trajectories': tracks,
            'signals': signals, 'boundaries': [boundary], 'observation_intervals': observations,
            'fixture_kind': 'SYNTHETIC_KNOWN_ORACLE_NOT_REAL_TRAFFIC'}


def verify_oracle(result):
    actual = {event['track_id']: event['classification'] for event in result['events']}
    errors = []
    if actual != EXPECTED_LABELS:
        errors.append('CLASSIFICATION_ORACLE_MISMATCH')
    if len(result['cells']) != 1 or any(result['cells'][0][field] != expected for field, expected in EXPECTED_COUNTS.items()):
        errors.append('COUNT_ORACLE_MISMATCH')
    times = {event['track_id']: event.get('t_cross_trajectory_s') for event in result['events']}
    if any(times.get(track) is None or abs(times[track]-expected) > 1e-10 for track, expected in EXPECTED_CROSSING_TIMES.items()):
        errors.append('CROSSING_TIME_ORACLE_MISMATCH')
    return {'kind': 'SYNTHETIC_MANUALLY_SPECIFIED_ORACLE', 'status': 'PASS' if not errors else 'FAIL', 'errors': errors,
            'expected_counts': EXPECTED_COUNTS, 'expected_labels': EXPECTED_LABELS,
            'expected_crossing_times_s': EXPECTED_CROSSING_TIMES,
            'real_accuracy': None, 'manual_real_review_status': 'NOT_PERFORMED'}
