"""Conservative finite-boundary entry classification on already normalized rows."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import math

from .contracts import ContractError, number, validate


@dataclass(frozen=True)
class EngineConfig:
    crossing_gap_max_s: float = 0.3
    boundary_hysteresis_m: float = 0.2
    clock_offset_s: float = 0.0
    boundary_offset_m: float = 0.0
    clock_uncertainty_s: float = 0.0

    def __post_init__(self):
        for name, value in asdict(self).items():
            number(value, name)
        if self.crossing_gap_max_s <= 0 or self.boundary_hysteresis_m < 0 or self.clock_uncertainty_s < 0:
            raise ContractError('INVALID_ENGINE_CONFIG')


def union_intervals(intervals):
    merged = []
    for lo, hi in sorted(intervals):
        if lo >= hi:
            continue
        if merged and lo <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(hi, merged[-1][1])
        else:
            merged.append((lo, hi))
    return merged


def intersection_intervals(a, b):
    return union_intervals((max(lo1, lo2), min(hi1, hi2)) for lo1, hi1 in union_intervals(a) for lo2, hi2 in union_intervals(b))


def intersection_seconds(a, b):
    return sum(hi-lo for lo, hi in intersection_intervals(a, b))


def classify_signal(lo, hi, signals):
    """Closed crossing-time range versus half-open signals, with explicit transition bounds.

    source_resolution_s records resolution and is not automatically added again
    as an error. Only transition_uncertainty_s expands signal transitions.
    """
    number(lo, 'crossing_lo'); number(hi, 'crossing_hi')
    if lo > hi:
        raise ContractError('INVALID_CROSSING_INTERVAL')
    states = set()
    guaranteed_coverage = []
    for row in signals:
        if row['state_basis'] != 'OBSERVED':
            continue
        uncertainty = row.get('transition_uncertainty_s', 0)
        start, end = row['t_start_s'], row['t_end_s']
        if hi >= start-uncertainty and lo < end+uncertainty:
            states.add(row['state'])
        guaranteed_coverage.append((start+uncertainty, end-uncertainty))
    if not any(start <= lo and hi < end for start, end in union_intervals(guaranteed_coverage)):
        states.add('UNKNOWN')
    if states == {'RED'}:
        label = 'RED_CONSISTENT'
    elif states and states <= {'GREEN', 'YELLOW'}:
        label = 'NON_RED_CONSISTENT'
    else:
        label = 'SIGNAL_AMBIGUOUS'
    return label, sorted(states)


def _point(row, reference):
    if row['position_reference'] != 'CENTROID':
        return None
    x, y = row['x_m'], row['y_m']
    if reference == 'CENTROID':
        return x, y
    if (row.get('body_direction_basis') != 'DOCUMENTED_FORWARD_AXIS' or
        row.get('yaw_rad') is None or row.get('length_m') is None or row['length_m'] <= 0):
        return None
    return x + 0.5*row['length_m']*math.cos(row['yaw_rad']), y + 0.5*row['length_m']*math.sin(row['yaw_rad'])


def crossing(rows, boundary, config):
    points = [_point(row, boundary['crossing_reference']) for row in rows]
    if any(point is None for point in points):
        return None, 'POSITION_OR_FRONT_GEOMETRY_UNVERIFIED'
    nx, ny = boundary['inward_normal']
    a0, b0 = boundary['directed_line']
    a = a0[0]+nx*config.boundary_offset_m, a0[1]+ny*config.boundary_offset_m
    b = b0[0]+nx*config.boundary_offset_m, b0[1]+ny*config.boundary_offset_m
    signed = [nx*(pt[0]-a[0])+ny*(pt[1]-a[1]) for pt in points]
    if signed[0] >= 0:
        return None, 'LEFT_CENSORED'
    armed = signed[0] <= -config.boundary_hysteresis_m
    candidate = None
    reason = 'NO_CONFIRMED_ENTRY'
    dx, dy = b[0]-a[0], b[1]-a[1]
    segment_norm2 = dx*dx+dy*dy
    for index in range(1, len(rows)):
        d0, d1 = signed[index-1:index+1]
        t0, t1 = rows[index-1]['timestamp_s'], rows[index]['timestamp_s']
        gap = t1-t0
        if gap > config.crossing_gap_max_s + 1e-12:
            if d0 < 0 <= d1 or candidate is not None:
                return None, 'CROSSING_GAP_EXCEEDED'
            armed = d1 <= -config.boundary_hysteresis_m
            candidate = None
            continue
        if d1 <= -config.boundary_hysteresis_m:
            armed = True
            candidate = None
        if d0 < 0 <= d1 and armed:
            fraction = -d0/(d1-d0)
            pt0, pt1 = points[index-1:index+1]
            px, py = pt0[0]+fraction*(pt1[0]-pt0[0]), pt0[1]+fraction*(pt1[1]-pt0[1])
            along = ((px-a[0])*dx+(py-a[1])*dy)/segment_norm2
            if not -1e-9 <= along <= 1+1e-9:
                reason = 'OUTSIDE_FINITE_SEGMENT'
                armed = False
                candidate = None
                continue
            candidate = {'t_cross_trajectory_s': t0+fraction*gap, 't_lo_trajectory_s': t0,
                         't_hi_trajectory_s': t1, 'source_row_ids': [rows[index-1]['source_row_id'], rows[index]['source_row_id']],
                         'crossing_point_m': [px, py]}
        if candidate is not None and d1 >= config.boundary_hysteresis_m:
            return candidate, None
    return None, reason


def _base_event(record, track_key, boundary):
    # A JSON tuple is collision free even if source identifiers contain separators.
    key = [*track_key, boundary['boundary_id'], boundary['profile_version'], boundary['crossing_reference']]
    return {'event_key': key, 'dataset_id': track_key[0], 'record_id': track_key[1], 'track_id': track_key[2],
            'site_id': record['site_id'], 'approach_id': boundary['approach_id'], 'boundary_id': boundary['boundary_id'],
            'profile_version': boundary['profile_version'], 'crossing_reference': boundary['crossing_reference'],
            'line_kind': boundary['line_kind'], 'data_kind': record['data_kind']}


def _exclusion(record, rows, boundary):
    if record['unit_basis'] != 'METERS_SECONDS' or record['coordinate_frame'] == 'UNKNOWN':
        return 'COORDINATE_OR_TIME_UNITS_UNVERIFIED'
    if record['coordinate_frame'] != boundary['coordinate_frame']:
        return 'COORDINATE_FRAME_MISMATCH'
    if record.get('time_alignment_status') not in ('DOCUMENTED', 'ASSUMPTION_BASED'):
        return 'TIME_ALIGNMENT_UNRESOLVED'
    if boundary['mapping_status'] != 'VERIFIED':
        return 'MOVEMENT_SIGNAL_MAPPING_UNVERIFIED'
    if boundary['movement'] != 'STRAIGHT' or any(row.get('movement') != 'STRAIGHT' for row in rows):
        return 'NON_STRAIGHT_OR_UNKNOWN_MOVEMENT'
    if any(row.get('movement_basis') not in ('VERIFIED_MAP', 'DOCUMENTED_PROVIDER', 'SYNTHETIC_TRUTH') for row in rows):
        return 'MOVEMENT_BASIS_UNVERIFIED'
    if any(row['class'] not in ('CAR', 'TRUCK', 'BUS', 'MOTORCYCLE', 'VEHICLE') for row in rows):
        return 'NON_VEHICLE_CLASS'
    lo, hi = boundary['validity_interval']
    if any(not lo <= row['timestamp_s'] < hi for row in rows):
        return 'BOUNDARY_OUTSIDE_VALIDITY'
    if any(a['timestamp_s'] >= b['timestamp_s'] for a, b in zip(rows, rows[1:])):
        return 'TRACK_TIME_ORDER_OR_DUPLICATE_CONFLICT'
    if len({row['approach_id'] for row in rows}) != 1:
        return 'APPROACH_ASSIGNMENT_CONFLICT'
    return None


def analyze(data, config=None):
    """Return one classification/exclusion per record-track-boundary profile.

    This API consumes verified normalized coordinates and mapping. It neither
    creates a boundary from imagery nor derives unobserved signal states.
    """
    config = config or EngineConfig()
    records, tracks, groups = validate(data)
    events = []
    for track_key, rows in sorted(tracks.items()):
        record = records[track_key[:2]]
        selected = [boundary for boundary in data['boundaries'] if boundary['dataset_id'] == track_key[0]
                    and boundary['site_id'] == record['site_id'] and boundary['approach_id'] in {r['approach_id'] for r in rows}]
        for boundary in selected:
            event = _base_event(record, track_key, boundary)
            reason = _exclusion(record, rows, boundary)
            signals = groups.get((*track_key[:2], boundary['signal_group_id']), [])
            if not reason and any(s['state_basis'] == 'PLAN' for s in signals):
                reason = 'PLAN_SIGNAL_NOT_OBSERVATION'
            if not reason and not any(s['state_basis'] == 'OBSERVED' for s in signals):
                reason = 'NO_OBSERVED_SIGNAL_GROUP'
            crossed = None
            if not reason:
                crossed, reason = crossing(rows, boundary, config)
            if reason:
                event.update(classification='EXCLUDED', excluded_reason=reason)
            else:
                timestamp_error = record.get('timestamp_uncertainty_s', 0)
                # Stable summation keeps exact decimal-boundary examples from
                # acquiring a spurious extra ULP through sequential subtraction.
                lo = math.fsum([crossed['t_lo_trajectory_s'], config.clock_offset_s, -timestamp_error, -config.clock_uncertainty_s])
                hi = math.fsum([crossed['t_hi_trajectory_s'], config.clock_offset_s, timestamp_error, config.clock_uncertainty_s])
                label, states = classify_signal(lo, hi, signals)
                scope = 'ASSUMPTION_BASED' if (record['data_kind'] == 'SYNTHETIC' or record['time_alignment_status'] != 'DOCUMENTED'
                        or config.clock_offset_s != 0 or config.boundary_offset_m != 0 or config.clock_uncertainty_s != 0) else 'DOCUMENTED_OBSERVATION'
                event.update(crossed)
                event.update(classification=label, excluded_reason=None,
                             t_cross_aligned_s=math.fsum([crossed['t_cross_trajectory_s'], config.clock_offset_s]),
                             t_lo_aligned_s=lo, t_hi_aligned_s=hi, possible_signal_states=states, classification_scope=scope,
                             uncertainty_components={'crossing_interval_basis': 'ADJACENT_SAMPLE_BRACKET',
                                'trajectory_timestamp_uncertainty_s': timestamp_error, 'relative_clock_uncertainty_s': config.clock_uncertainty_s,
                                'scenario_clock_offset_s': config.clock_offset_s,
                                'signal_transition_uncertainty_s': sorted({s.get('transition_uncertainty_s', 0) for s in signals}),
                                'source_resolution_used_as_additional_error': False})
            events.append(event)
    # Unmapped tracks are explicitly recorded; they never disappear from QC.
    mapped = {tuple(event['event_key'][:3]) for event in events}
    for track_key, rows in sorted(tracks.items()):
        if track_key not in mapped:
            record = records[track_key[:2]]
            events.append({'event_key': [*track_key, None, None, None], 'dataset_id': track_key[0], 'record_id': track_key[1],
                           'track_id': track_key[2], 'site_id': record['site_id'], 'approach_id': rows[0]['approach_id'],
                           'boundary_id': None, 'profile_version': None, 'crossing_reference': None, 'line_kind': None,
                           'data_kind': record['data_kind'], 'classification': 'EXCLUDED', 'excluded_reason': 'NO_VERIFIED_BOUNDARY_PROFILE'})
    return {'schema_version': 'spl-baseline-1', 'data_kind': next(iter({r['data_kind'] for r in records.values()}), None),
            'config': asdict(config), 'events': events, 'cells': aggregate(events, data, groups),
            'manual_review_status': 'NOT_PERFORMED', 'accuracy': None, 'ai_status': 'NOT_RUN',
            'ai_reason': 'BOUNDED_BASELINE_ONLY_REAL_READINESS_PENDING',
            'limitations': ['Sample-bracket interpolation is not increased temporal precision.',
                            'Classification bounds are not a 95% confidence interval.',
                            'RED_CONSISTENT is not legal violation or accident probability.',
                            'No AI, population bootstrap, priority rankings or source-specific real adapter implemented.']}


CELL_FIELDS = ('dataset_id', 'site_id', 'approach_id', 'record_id', 'boundary_id', 'profile_version', 'crossing_reference', 'line_kind', 'data_kind')


def aggregate(events, data=None, groups=None):
    grouped = {}
    for event in events:
        key = tuple(event[field] for field in CELL_FIELDS)
        grouped.setdefault(key, []).append(event)
    cells = []
    for key, rows in sorted(grouped.items(), key=lambda pair: repr(pair[0])):
        counts = Counter(row['classification'] for row in rows)
        c, g, u = [counts[label] for label in ('RED_CONSISTENT', 'NON_RED_CONSISTENT', 'SIGNAL_AMBIGUOUS')]
        n = c+g+u
        excluded = counts['EXCLUDED']
        cell = dict(zip(CELL_FIELDS, key))
        cell.update(N=n, C=c, G=g, U=u, excluded_count=excluded, inspected_track_boundary_count=len(rows),
                    excluded_reason_counts=dict(sorted(Counter(row['excluded_reason'] for row in rows if row['classification'] == 'EXCLUDED').items())),
                    rate_lower_per_1000=1000*c/n if n else None, rate_upper_per_1000=1000*(c+u)/n if n else None,
                    ambiguous_fraction=u/n if n else None, excluded_fraction=excluded/len(rows) if rows else None,
                    uncertainty_range_kind='CLASSIFICATION_BOUNDS_NOT_95_PERCENT_CI', population_ci_95=None,
                    population_ci_reason='NOT_IMPLEMENTED_NO_POPULATION_INFERENCE', priority_tag='DESCRIPTIVE_ONLY', rank=None,
                    valid_observation_seconds=None, red_observation_seconds=None, count_per_hour=None,
                    exposure_red_count=None, exposure_uncovered_red_count=None,
                    exposure_status='MISSING_VERIFIED_OBSERVATION_INTERVALS', exposure_blocked_reason=None)
        if data is not None and key[4] is not None:
            boundaries = [b for b in data['boundaries'] if b['dataset_id'] == key[0] and b['site_id'] == key[1]
                          and b['approach_id'] == key[2] and b['boundary_id'] == key[4] and b['profile_version'] == key[5]]
            observed = [(r['t_start_s'], r['t_end_s']) for r in data.get('observation_intervals', [])
                        if (r['dataset_id'], r['record_id'], r['approach_id']) == (key[0], key[3], key[2]) and r.get('status') == 'VERIFIED']
            signals = (groups or {}).get((key[0], key[3], boundaries[0]['signal_group_id']), []) if boundaries else []
            record = next((r for r in data['records'] if (r['dataset_id'], r['record_id']) == (key[0], key[3])), None)
            definition_reason = None
            if not boundaries or boundaries[0]['mapping_status'] != 'VERIFIED' or boundaries[0]['movement'] != 'STRAIGHT':
                definition_reason = 'MOVEMENT_SIGNAL_MAPPING_UNVERIFIED'
            elif not record or record['unit_basis'] != 'METERS_SECONDS' or record['coordinate_frame'] == 'UNKNOWN':
                definition_reason = 'COORDINATE_OR_TIME_UNITS_UNVERIFIED'
            elif record['coordinate_frame'] != boundaries[0]['coordinate_frame']:
                definition_reason = 'COORDINATE_FRAME_MISMATCH'
            elif record.get('time_alignment_status') not in ('DOCUMENTED', 'ASSUMPTION_BASED'):
                definition_reason = 'TIME_ALIGNMENT_UNRESOLVED'
            elif any(signal['state_basis'] == 'PLAN' for signal in signals):
                definition_reason = 'PLAN_SIGNAL_NOT_OBSERVATION'
            if definition_reason:
                cell.update(exposure_status='UNVERIFIED_ANALYSIS_DEFINITION', exposure_blocked_reason=definition_reason)
            elif observed:
                known = [(s['t_start_s'], s['t_end_s']) for s in signals if s['state_basis'] == 'OBSERVED' and s['state'] != 'UNKNOWN']
                red = [(s['t_start_s'], s['t_end_s']) for s in signals if s['state_basis'] == 'OBSERVED' and s['state'] == 'RED']
                valid = intersection_intervals(observed, known)
                valid = intersection_intervals(valid, [tuple(boundaries[0]['validity_interval'])]) if boundaries else []
                seconds = sum(hi-lo for lo, hi in valid)
                covered_red = sum(row['classification'] == 'RED_CONSISTENT' and
                                  any(lo <= row['t_lo_aligned_s'] and row['t_hi_aligned_s'] < hi for lo, hi in valid)
                                  for row in rows)
                uncovered_red = c-covered_red
                # A full-record numerator must not be divided by partial exposure.
                # Preserve the full cohort's C and publish why intensity is null.
                status = 'NUMERATOR_OUTSIDE_EXPOSURE_COUNT_PER_HOUR_UNAVAILABLE' if uncovered_red else 'VERIFIED_INTERVAL_UNION_INTERSECT_OBSERVED_SIGNAL_AND_BOUNDARY_VALIDITY'
                cell.update(valid_observation_seconds=seconds, red_observation_seconds=intersection_seconds(valid, red),
                            count_per_hour=c*3600/seconds if seconds > 0 and uncovered_red == 0 else None,
                            exposure_red_count=covered_red, exposure_uncovered_red_count=uncovered_red, exposure_status=status)
        cells.append(cell)
    return cells


def sensitivity(data, clock_offsets=(-0.5, -0.2, 0, 0.2, 0.5), boundary_offsets=(-0.5, 0, 0.5), config=None):
    """Recompute each scenario; report intersection cohort rather than assume fixed N."""
    base = asdict(config or EngineConfig())
    results = []
    for clock in clock_offsets:
        for boundary in boundary_offsets:
            result = analyze(data, EngineConfig(**{**base, 'clock_offset_s': clock, 'boundary_offset_m': boundary}))
            valid = {tuple(e['event_key']) for e in result['events'] if e['classification'] != 'EXCLUDED'}
            results.append((clock, boundary, result, valid))
    common = set.intersection(*(row[3] for row in results)) if results else set()
    return {'basis': 'ASSUMED_SCENARIOS_NOT_MEASURED_ERROR_DISTRIBUTION', 'signal_clock_shifted': False,
            'common_event_keys': [list(key) for key in sorted(common, key=repr)],
            'scenarios': [{'clock_offset_s': clock, 'boundary_offset_m': boundary, 'cells': result['cells'],
                           'valid_event_count': len(valid), 'common_event_count': len(common),
                           'common_cohort_cells': aggregate([e for e in result['events'] if tuple(e['event_key']) in common])}
                          for clock, boundary, result, valid in results]}
