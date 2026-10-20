"""A bounded normalized JSON interchange contract, not a source-data adapter."""
from __future__ import annotations

import math
from collections import defaultdict


class ContractError(ValueError):
    pass


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError('NONFINITE_OR_NONNUMERIC_' + name)
    return float(value)


def text(row, field):
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ContractError('MISSING_' + field)
    return value


def interval(row, start='t_start_s', end='t_end_s'):
    lo, hi = number(row.get(start), start), number(row.get(end), end)
    if not lo < hi:
        raise ContractError('INVALID_INTERVAL')
    return lo, hi


def validate(data):
    if not isinstance(data, dict) or data.get('schema_version') != 'spl-normalized-1':
        raise ContractError('UNSUPPORTED_NORMALIZED_SCHEMA')
    for table in ('records', 'trajectories', 'signals', 'boundaries'):
        if not isinstance(data.get(table), list) or any(not isinstance(r, dict) for r in data[table]):
            raise ContractError('INVALID_TABLE_' + table)
    records = {}
    for row in data['records']:
        key = text(row, 'dataset_id'), text(row, 'record_id')
        if key in records:
            raise ContractError('DUPLICATE_RECORD_KEY')
        for field in ('source_version', 'site_id', 'data_kind', 'time_basis', 'coordinate_frame', 'unit_basis'):
            text(row, field)
        interval(row)
        if row['data_kind'] not in ('REAL', 'SYNTHETIC') or row['time_basis'] not in ('RECORD_RELATIVE', 'UTC'):
            raise ContractError('INVALID_RECORD_BASIS')
        if not isinstance(row.get('source_refs'), list) or not row['source_refs']:
            raise ContractError('MISSING_SOURCE_REFS')
        uncertainty = number(row.get('timestamp_uncertainty_s', 0), 'timestamp_uncertainty_s')
        if uncertainty < 0:
            raise ContractError('NEGATIVE_TIME_UNCERTAINTY')
        records[key] = row
    kinds = {row['data_kind'] for row in records.values()}
    if len(kinds) > 1:
        raise ContractError('REAL_SYNTHETIC_MIX_FORBIDDEN')
    if kinds == {'REAL'}:
        if isinstance(data.get('fixture_kind'), str) and 'SYNTHETIC' in data['fixture_kind'].upper():
            raise ContractError('REAL_RECORD_WITH_SYNTHETIC_FIXTURE_TAG')
        if any('src/spl/synthetic.py' in row['source_refs'] for row in records.values()):
            raise ContractError('REAL_RECORD_WITH_SYNTHETIC_SOURCE_REFERENCE')
        rights = data.get('rights', {})
        for dataset_id in {key[0] for key in records}:
            right = rights.get(dataset_id, {})
            if any(right.get(field) != 'ALLOW' for field in ('access_status', 'analysis_status')):
                raise ContractError('REAL_ANALYSIS_RIGHTS_NOT_ALLOW')
            if not right.get('license_evidence'):
                raise ContractError('REAL_LICENSE_EVIDENCE_MISSING')
    signal_groups = defaultdict(list)
    signal_keys = set()
    for row in data['signals']:
        key = text(row, 'dataset_id'), text(row, 'record_id')
        if key not in records:
            raise ContractError('SIGNAL_RECORD_FOREIGN_KEY')
        lo, hi = interval(row)
        rec = records[key]
        if lo < rec['t_start_s'] or hi > rec['t_end_s']:
            raise ContractError('SIGNAL_OUTSIDE_RECORD')
        group = text(row, 'signal_group_id')
        pk = (*key, group, lo)
        if pk in signal_keys:
            raise ContractError('DUPLICATE_SIGNAL_KEY')
        signal_keys.add(pk)
        if row.get('state') not in ('RED', 'GREEN', 'YELLOW', 'UNKNOWN'):
            raise ContractError('INVALID_SIGNAL_STATE')
        if row.get('state_basis') not in ('OBSERVED', 'PLAN', 'UNKNOWN'):
            raise ContractError('INVALID_SIGNAL_BASIS')
        for field in ('source_resolution_s', 'transition_uncertainty_s'):
            if number(row.get(field, 0), field) < 0:
                raise ContractError('NEGATIVE_SIGNAL_UNCERTAINTY')
        if not row.get('source_row_ids'):
            raise ContractError('MISSING_SIGNAL_SOURCE_ROWS')
        signal_groups[(*key, group)].append(row)
    for rows in signal_groups.values():
        rows.sort(key=lambda r: r['t_start_s'])
        if any(a['t_end_s'] > b['t_start_s'] for a, b in zip(rows, rows[1:])):
            raise ContractError('OVERLAPPING_SIGNAL_INTERVALS')
    boundaries = set()
    for row in data['boundaries']:
        for field in ('dataset_id', 'site_id', 'approach_id', 'boundary_id', 'profile_version',
                      'movement', 'signal_group_id', 'coordinate_frame', 'line_kind', 'crossing_reference', 'mapping_status'):
            text(row, field)
        pk = tuple(row[f] for f in ('dataset_id', 'site_id', 'approach_id', 'boundary_id', 'profile_version'))
        if pk in boundaries:
            raise ContractError('DUPLICATE_BOUNDARY_KEY')
        boundaries.add(pk)
        if row['crossing_reference'] not in ('FRONT_BUMPER', 'CENTROID'):
            raise ContractError('INVALID_CROSSING_REFERENCE')
        points, normal = row.get('directed_line'), row.get('inward_normal')
        if not isinstance(points, list) or len(points) != 2 or any(not isinstance(pt, list) or len(pt) != 2 for pt in points):
            raise ContractError('INVALID_DIRECTED_LINE')
        if not isinstance(normal, list) or len(normal) != 2:
            raise ContractError('INVALID_INWARD_NORMAL')
        a, b = [tuple(number(v, 'line_coordinate') for v in pt) for pt in points]
        nx, ny = [number(v, 'normal') for v in normal]
        dx, dy = b[0] - a[0], b[1] - a[1]
        if math.hypot(dx, dy) == 0 or abs(math.hypot(nx, ny) - 1) > 1e-6 or abs(nx*dx + ny*dy) > 1e-6 * math.hypot(dx, dy):
            raise ContractError('DEGENERATE_OR_NONPERPENDICULAR_BOUNDARY')
        validity = row.get('validity_interval')
        if not isinstance(validity, list) or len(validity) != 2 or number(validity[0], 'validity_start') >= number(validity[1], 'validity_end'):
            raise ContractError('INVALID_BOUNDARY_VALIDITY')
        if not row.get('evidence_refs'):
            raise ContractError('MISSING_BOUNDARY_EVIDENCE')
    tracks = defaultdict(list)
    for row in data['trajectories']:
        key = text(row, 'dataset_id'), text(row, 'record_id')
        if key not in records:
            raise ContractError('TRAJECTORY_RECORD_FOREIGN_KEY')
        for field in ('track_id', 'site_id', 'class', 'position_reference', 'source_row_id', 'approach_id'):
            text(row, field)
        for field in ('timestamp_s', 'x_m', 'y_m'):
            number(row.get(field), field)
        for field in ('yaw_rad', 'length_m'):
            if row.get(field) is not None:
                number(row[field], field)
        rec = records[key]
        if rec['data_kind'] == 'REAL' and row.get('movement_basis') == 'SYNTHETIC_TRUTH':
            raise ContractError('REAL_TRAJECTORY_WITH_SYNTHETIC_TRUTH_BASIS')
        if row['site_id'] != rec['site_id']:
            raise ContractError('TRAJECTORY_SITE_FOREIGN_KEY')
        if not rec['t_start_s'] <= row['timestamp_s'] <= rec['t_end_s']:
            raise ContractError('TRAJECTORY_OUTSIDE_RECORD')
        tracks[(*key, row['track_id'])].append(row)
    for row in data.get('observation_intervals', []):
        key = text(row, 'dataset_id'), text(row, 'record_id')
        text(row, 'approach_id')
        lo, hi = interval(row)
        if key not in records or lo < records[key]['t_start_s'] or hi > records[key]['t_end_s']:
            raise ContractError('OBSERVATION_RECORD_FOREIGN_KEY_OR_RANGE')
    return records, tracks, signal_groups
