"""SYNTHETIC tests of quality holds; no real replay, model or cache is used."""
from copy import deepcopy
import unittest

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from spl.r2_signal_quality import analyze_boundary, QUALITY
from spl.r2_signals import CONDITION_STATES, OFFSETS, POPULATIONS

LO, HI = 10000, 20000
MAP = {'S': {'encroaching_signal_group_id': 'E', 'priority_signal_group_id': 'P'}}


def ledger(rows):
    return pd.DataFrame([{'event_key': key, 'scenario_id': scenario, 'pet_s': pet,
                          'qc_status': 'VALID_NONNEGATIVE_PET' if pet >= 0 else 'NEGATIVE_PET_REVIEW',
                          'ts_enter_encroaching_ms': enc, 'ts_enter_priority_ms': priority,
                          'fixture_kind': 'SYNTHETIC_NOT_REAL_TRAFFIC'}
                         for key, scenario, pet, enc, priority in rows],
                        columns=['event_key', 'scenario_id', 'pet_s', 'qc_status',
                                 'ts_enter_encroaching_ms', 'ts_enter_priority_ms', 'fixture_kind'])


def crossings(segments=(('E', 9000, 21000), ('P', 8000, 22000))):
    return pd.DataFrame([{'signal_group_id': g, 'start_timestamp_ms': a, 'end_timestamp_ms': b,
                          'candidate_period_start_ms': LO, 'candidate_period_end_ms': HI,
                          'source_row_id': i, 'signal_state': 'red', 'claim_status': 'SYNTHETIC_UNRESOLVED'}
                         for i, (g, a, b) in enumerate(segments)])


def conditions(events, pairs=None, scenarios=('S',), thresholds=(.5, 1.)):
    """Independent saved-literal fixture; never calls the tested boundary code."""
    pairs = pairs or {}
    rows = []
    for scenario in scenarios:
        for enc in CONDITION_STATES:
            for priority in CONDITION_STATES:
                selected = [r for r in events.to_dict('records')
                            if r['qc_status'] == 'VALID_NONNEGATIVE_PET' and
                            (scenario == 'ALL_SCENARIOS' or r['scenario_id'] == scenario) and
                            pairs.get(r['event_key'], ('green', 'red')) == (enc, priority)]
                old = [r for r in selected if any(v is not None and not pd.isna(v) and LO <= int(v) < HI
                                                 for v in (r['ts_enter_encroaching_ms'], r['ts_enter_priority_ms']))]
                for tau in thresholds:
                    n, short = len(selected), sum(r['pet_s'] <= tau for r in selected)
                    rows.append({'scenario_id': scenario, 'encroaching_entry_signal': enc,
                                 'priority_entry_signal': priority, 'tau_s': tau, 'clock_offset_s': 0.,
                                 'n_valid_nonnegative_pet': n, 'n_short_pet': short,
                                 'short_pet_fraction': short / n if n else None,
                                 'candidate_period_n_valid_nonnegative_pet': len(old),
                                 'candidate_period_n_short_pet': sum(r['pet_s'] <= tau for r in old),
                                 'original_extra_column': 'SYNTHETIC_PRESERVED'})
    return pd.DataFrame(rows)


class BoundaryQualityTests(unittest.TestCase):
    def analyze(self, events, intervals=None, mapping=None, literal=None, pairs=None, **kw):
        intervals = crossings() if intervals is None else intervals
        mapping = deepcopy(MAP) if mapping is None else mapping
        literal = conditions(events, pairs) if literal is None else literal
        calls = []
        def lookup(group, timestamp):
            calls.append((group, timestamp))
            return {'state': 'green' if group == 'E' else 'red'}
        result = analyze_boundary(events, intervals, mapping, literal, kw.pop('lookup', lookup), **kw)
        return result, calls

    def impact(self, result, population=POPULATIONS[0], offset=0., scenario='ALL_SCENARIOS'):
        table = result['tables']['boundary_impact']
        return table.loc[(table['population'] == population) & (table['clock_offset_s'] == offset) &
                         (table['scenario_id'] == scenario)].iloc[0]

    def cell(self, result, enc='green', priority='red', scenario='S', tau=1.):
        table = result['tables']['signal_condition_quality']
        return table.loc[(table['scenario_id'] == scenario) & (table['encroaching_entry_signal'] == enc) &
                         (table['priority_entry_signal'] == priority) & (table['tau_s'] == tau)].iloc[0]

    def test_exact_start_minus_one_start_end_minus_one_end_half_open(self):
        data = ledger([(str(i), 'S', .5, t, 50000) for i, t in enumerate((8999, 9000, 20999, 21000))])
        result, calls = self.analyze(data, crossings((('E', 9000, 21000),)), offsets_s=[0])
        row = self.impact(result)
        self.assertEqual(row['left_tail_unique_events'], 1)
        self.assertEqual(row['right_tail_unique_events'], 1)
        self.assertEqual(row['long_interval_held_unique_events'], 2)
        self.assertEqual(row['long_interval_held_role_queries'], 2)
        self.assertEqual(row['tail_unique_events'], 2)
        self.assertEqual(len(calls), 4)
        quality = result['tables']['quality_intervals']
        self.assertEqual(quality.iloc[0]['start_timestamp_ms'], 9000)
        self.assertEqual(quality.iloc[0]['end_timestamp_ms'], 21000)
        self.assertTrue(quality['source_quality_status'].eq(QUALITY).all())

    def test_groups_own_roles_dual_role_unique_event_dedup(self):
        data = ledger([('both', 'S', .5, 9500, 21500), ('unrelated', 'T', .5, 9500, 21500)])
        mapping = {**MAP, 'T': {'encroaching_signal_group_id': 'X', 'priority_signal_group_id': 'Y'}}
        result, _ = self.analyze(data, mapping=mapping, literal=conditions(data, scenarios=('S', 'T')), offsets_s=[0])
        row = self.impact(result)
        self.assertEqual(row['left_tail_role_queries'], 1)
        self.assertEqual(row['right_tail_role_queries'], 1)
        self.assertEqual(row['tail_role_queries'], 2)
        self.assertEqual(row['tail_unique_events'], 1)
        self.assertEqual(row['long_interval_held_unique_events'], 1)
        self.assertEqual(self.impact(result, scenario='T')['long_interval_held_unique_events'], 0)
        roles = result['tables']['boundary_role_impact']
        for field in ('left_tail', 'right_tail', 'long_interval_held', 'original_date_hold_own_entry'):
            self.assertTrue((roles[field + '_role_queries'] == roles[field + '_unique_events']).all())
        self.assertEqual(roles.loc[roles['population'] == POPULATIONS[0], 'long_interval_held_role_queries'].sum(), 2)

    def test_overlapping_duplicate_crossings_do_not_multiply_membership(self):
        data = ledger([('one', 'S', .5, 9500, 30000)])
        result, _ = self.analyze(data, crossings((('E', 9000, 21000), ('E', 8500, 21500))), offsets_s=[0])
        self.assertEqual(self.impact(result)['long_interval_held_role_queries'], 1)
        self.assertEqual(self.impact(result)['long_interval_held_unique_events'], 1)
        self.assertEqual(result['summary']['n_quality_interval_rows'], 2)

    def test_original_date_hold_is_any_original_role_and_fixed_across_offsets(self):
        data = ledger([('old', 'S', .5, 9999, 10000), ('none', 'S', 2., 50000, 50000)])
        result, calls = self.analyze(data, crossings((('E', 9900, 10100),)), offsets_s=[-.2, 0, .2])
        for offset in (-.2, 0., .2):
            self.assertEqual(self.impact(result, offset=offset)['original_date_hold_unique_events'], 1)
            self.assertEqual(self.impact(result, offset=offset)['union_held_unique_events'], 1)
        cell = self.cell(result)
        self.assertEqual(cell['quality_held_n_valid_nonnegative_pet'], 1)
        self.assertEqual(cell['interpretable_n_valid_nonnegative_pet'], 1)
        self.assertEqual(calls, [])

    def test_nine_offsets_common_fixed_populations_negative_and_short_distinct(self):
        data = ledger([('short', 'S', 1., 9000, 30000), ('negative', 'S', -.1, 9000, 30000),
                       ('long', 'S', 1.001, 21000, 30000)])
        result, calls = self.analyze(data)
        expected = {POPULATIONS[0]: 3, POPULATIONS[1]: 2, POPULATIONS[2]: 1}
        for offset in OFFSETS:
            for population, n in expected.items():
                self.assertEqual(self.impact(result, population, offset)['fixed_population_events'], n)
        self.assertEqual(self.impact(result)['tail_unique_events'], 2)
        self.assertEqual(self.impact(result, POPULATIONS[1])['tail_unique_events'], 1)
        self.assertEqual(self.impact(result, POPULATIONS[2])['tail_unique_events'], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['summary']['n_newly_held_nonnegative_events_offset_zero'], 1)

    def test_literal_partition_quality_remaining_unknown_and_interpretable_null(self):
        data = ledger([('new', 'S', .5, 9500, 30000), ('old', 'S', .8, 15000, 30000),
                       ('known', 'S', 2., 30000, 30000), ('unknown', 'S', .4, 40000, 40000)])
        pairs = {'unknown': ('UNKNOWN', 'SIGNAL_AMBIGUOUS')}
        result, _ = self.analyze(data, pairs=pairs, offsets_s=[0])
        known = self.cell(result)
        self.assertEqual(known['n_valid_nonnegative_pet'], 3)
        self.assertEqual(known['quality_held_n_valid_nonnegative_pet'], 2)
        self.assertEqual(known['quality_held_n_short_pet'], 2)
        self.assertEqual(known['interpretable_n_valid_nonnegative_pet'], 1)
        self.assertEqual(known['interpretable_n_short_pet'], 0)
        unknown = self.cell(result, 'UNKNOWN', 'SIGNAL_AMBIGUOUS')
        self.assertEqual(unknown['remaining_unknown_n_valid_nonnegative_pet'], 1)
        self.assertEqual(unknown['interpretable_n_valid_nonnegative_pet'], 0)
        self.assertTrue(pd.isna(unknown['interpretable_short_pet_fraction']))
        empty = self.cell(result, 'red', 'green')
        self.assertTrue(pd.isna(empty['interpretable_short_pet_fraction']))
        table = result['tables']['signal_condition_quality']
        for original, suffix in (('n_valid_nonnegative_pet', 'n_valid_nonnegative_pet'), ('n_short_pet', 'n_short_pet')):
            self.assertTrue((table[original] == table['quality_held_' + suffix] + table['remaining_unknown_' + suffix] + table['interpretable_' + suffix]).all())

    def test_lookup_only_new_held_nonnegative_with_original_cell_counts_reused(self):
        data = ledger([('new', 'S', .5, 9500, 30000), ('old', 'S', .5, 15000, 30000),
                       ('negative', 'S', -.1, 9500, 30000), ('outside', 'S', .5, 50000, 50000)])
        result, calls = self.analyze(data)
        self.assertEqual(calls, [('E', 9500), ('P', 30000)])
        self.assertEqual(result['summary']['literal_lookup_calls'], 2)
        self.assertEqual(self.cell(result)['candidate_period_n_valid_nonnegative_pet'], 1)
        self.assertEqual(self.cell(result)['new_long_interval_held_n_valid_nonnegative_pet'], 1)

    def test_new_held_unknown_pair_is_quality_not_remaining_unknown(self):
        data = ledger([('new', 'S', .5, 9500, 30000)])
        pairs = {'new': ('UNKNOWN', 'red')}
        def lookup(group, timestamp):
            return {'state': 'UNKNOWN' if group == 'E' else 'red'}
        result, _ = self.analyze(data, pairs=pairs, offsets_s=[0], lookup=lookup)
        cell = self.cell(result, 'UNKNOWN', 'red')
        self.assertEqual(cell['quality_held_n_valid_nonnegative_pet'], 1)
        self.assertEqual(cell['remaining_unknown_n_valid_nonnegative_pet'], 0)
        self.assertEqual(cell['interpretable_n_valid_nonnegative_pet'], 0)

    def test_original_dataframes_mappings_and_all_literal_columns_preserved(self):
        data = ledger([('new', 'S', .5, 9500, 30000)])
        source = crossings();literal = conditions(data);mapping = deepcopy(MAP)
        before = (data.copy(deep=True), source.copy(deep=True), literal.copy(deep=True), deepcopy(mapping))
        result, _ = self.analyze(data, source, mapping, literal, offsets_s=[0])
        assert_frame_equal(data, before[0]);assert_frame_equal(source, before[1]);assert_frame_equal(literal, before[2])
        self.assertEqual(mapping, before[3])
        assert_frame_equal(result['tables']['signal_condition_quality'][literal.columns], literal)
        assert_frame_equal(result['tables']['quality_intervals'][source.columns], source)

    def test_exact_integer_strings_and_invalid_row_null_clock_no_guess_or_lookup(self):
        data = ledger([('new', 'S', .5, '9500', '30000'), ('null', 'S', .5, None, None)])
        data.loc[data['event_key'] == 'null', 'qc_status'] = 'INVALID'
        pairs = {'null': ('UNKNOWN', 'UNKNOWN')}
        result, calls = self.analyze(data, pairs=pairs, offsets_s=[0])
        self.assertEqual(calls, [('E', 9500), ('P', 30000)])
        self.assertEqual(result['summary']['n_null_role_timestamps'], 2)
        self.assertEqual(self.cell(result, 'UNKNOWN', 'UNKNOWN')['remaining_unknown_n_valid_nonnegative_pet'], 0)

    def test_qc_valid_signed_null_entry_rejected_even_with_known_literal_cell(self):
        for pet in (.5, -.1):
            for role in ('encroaching', 'priority'):
                with self.subTest(pet=pet, role=role):
                    data = ledger([('one', 'S', pet, 30000, 30000)])
                    literal = conditions(data)
                    data['ts_enter_' + role + '_ms'] = pd.Series([None], dtype=object)
                    with self.assertRaisesRegex(ValueError, 'QC-valid signed null own-entry'):
                        self.analyze(data, literal=literal, offsets_s=[0])

    def test_zero_population_and_unused_groups_retained_as_null(self):
        data = ledger([('negative', 'S', -.1, 9500, 30000)])
        result, calls = self.analyze(data, offsets_s=[0])
        row = self.impact(result, POPULATIONS[1])
        self.assertEqual(row['fixed_population_events'], 0)
        self.assertTrue(pd.isna(row['union_held_event_fraction']))
        roles = result['tables']['boundary_role_impact']
        self.assertTrue(set(roles['signal_group_id']) >= {'E', 'P'})
        self.assertEqual(calls, [])

    def test_invalid_timestamps_offsets_keys_qc_and_interval_contract_rejected(self):
        original = ledger([('one', 'S', .5, 9500, 30000)])
        literal = conditions(original)
        for value in ('9.5e3', '9500.0', True, 9500.5, 2**63):
            with self.subTest(value=value):
                data = original.copy();data['ts_enter_encroaching_ms'] = pd.Series([value], dtype=object)
                with self.assertRaises(ValueError):self.analyze(data, literal=literal, offsets_s=[0])
        for offsets in ([1], [0, 0], [0, .0001], [0, float('nan')]):
            with self.subTest(offsets=offsets):
                with self.assertRaises(ValueError):self.analyze(original, offsets_s=offsets)
        with self.assertRaises(ValueError):self.analyze(pd.concat((original, original)), literal=literal, offsets_s=[0])
        data = original.copy();data['qc_status'] = 'UNKNOWN'
        with self.assertRaises(ValueError):self.analyze(data, literal=literal, offsets_s=[0])
        data = original.copy();data['pet_s'] = -.1
        with self.assertRaises(ValueError):self.analyze(data, literal=literal, offsets_s=[0])
        for column, value in (('start_timestamp_ms', 21000), ('candidate_period_end_ms', LO), ('start_timestamp_ms', '9e3')):
            bad = crossings();bad[column] = value
            with self.subTest(column=column, value=value):
                with self.assertRaises(ValueError):self.analyze(original, bad, literal=literal, offsets_s=[0])

    def test_stale_old_date_counts_literal_counts_lookup_and_duplicate_cells_rejected(self):
        data = ledger([('old', 'S', .5, 15000, 30000), ('new', 'S', .5, 9500, 30000)])
        literal = conditions(data)
        index = literal.index[(literal['encroaching_entry_signal'] == 'green') & (literal['priority_entry_signal'] == 'red')][0]
        for column in ('candidate_period_n_valid_nonnegative_pet', 'n_valid_nonnegative_pet'):
            bad = literal.copy();bad.loc[index, column] += 1
            with self.subTest(column=column):
                with self.assertRaises(ValueError):self.analyze(data, literal=bad, offsets_s=[0])
        with self.assertRaises(ValueError):self.analyze(data, literal=pd.concat((literal, literal.iloc[:1])), offsets_s=[0])
        with self.assertRaises(ValueError):self.analyze(data, literal=literal, offsets_s=[0], lookup=lambda g, t: {'state': 'normal'})

    def test_optional_all_scenario_literal_aggregate_and_input_order_invariance(self):
        data = ledger([('new', 'S', .5, 9500, 30000), ('old', 'S', 2., 15000, 30000)])
        literal = conditions(data, scenarios=('S', 'ALL_SCENARIOS'))
        first, _ = self.analyze(data, literal=literal, offsets_s=[0])
        second, _ = self.analyze(data.iloc[::-1], literal=literal, offsets_s=[0])
        for name in first['tables']:
            assert_frame_equal(first['tables'][name], second['tables'][name])
        self.assertEqual(self.cell(first, scenario='ALL_SCENARIOS')['quality_held_n_valid_nonnegative_pet'], 2)


if __name__ == '__main__':
    unittest.main()
