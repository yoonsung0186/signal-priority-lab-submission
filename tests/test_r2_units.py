"""Synthetic R2 unit/denominator regressions, no fits or full baseline reruns."""
import unittest
import numpy as np
import pandas as pd
from spl.r2_units import analyze_units, prepare_ledger, _ranks


def event(identifier, pet=1, pair=('e', 'p'), scenario='S', timestamp=1000, date='2025-01-01', invalid=False, nsource=1):
    return {'event_key': 'SYNTHETIC/TEST/' + str(identifier), 'event_id': str(identifier), 'scenario_id': scenario,
            'encroaching_object_id': pair[0], 'priority_object_id': pair[1], 'pet_s': pet,
            'qc_status': 'INVALID' if invalid else 'NEGATIVE_PET_REVIEW' if pet < 0 else 'VALID_NONNEGATIVE_PET',
            'pet_eligible': not invalid, 'event_date_local': date, 'n_source_rows': nsource,
            'ts_enter_encroaching_ms': timestamp, 'ts_leave_encroaching_ms': timestamp + 100,
            'ts_enter_priority_ms': timestamp + 200, 'ts_leave_priority_ms': timestamp + 300,
            'conflict_lon_deg': 13., 'conflict_lat_deg': 47., 'data_kind': 'SYNTHETIC'}


def summary(result, unit, scenario='S', tau=1):
    table = result['tables']['unit_summary']
    return table.loc[table['unit'].eq(unit) & table['scenario_id'].eq(scenario) & table['tau_s'].eq(tau)].iloc[0]


class R2UnitTests(unittest.TestCase):
    def test_negative_first_never_replaced_and_D_selects_then_filters(self):
        data = pd.DataFrame([event(1, -.1), event(2, .2, timestamp=2000), event(3, 8, pair=('q', 'r'), timestamp=3000), event(4, .1, pair=('q', 'r'), timestamp=4000)])
        result = analyze_units(data)
        self.assertEqual(summary(result, 'A')['denominator'], 3)
        self.assertEqual(summary(result, 'B')['denominator'], 2)
        c = summary(result, 'C'); self.assertEqual((c['n_selected'], c['n_negative'], c['denominator'], c['numerator']), (2, 1, 1, 0))
        d = summary(result, 'D'); self.assertEqual(d['n_selected'], 0); self.assertTrue(pd.isna(d['fraction']))
        self.assertEqual(d['n_negative_first_representatives_before_filter'], 1)
        selection = result['tables']['unit_selection'].set_index('event_id')
        self.assertTrue(selection.loc['1', 'selected_C']); self.assertFalse(selection.loc['2', 'selected_C'])
        self.assertFalse(selection.loc['4', 'selected_D'])

    def test_representative_timestamp_then_numeric_event_id_then_key(self):
        data = pd.DataFrame([event(10, .1), event(2, 9), event(1, .2, timestamp=2000), event(20, .5, pair=('z', 'w'))])
        frame, _ = prepare_ledger(data)
        self.assertEqual(set(frame.loc[frame['_C'], 'event_id']), {'2', '20'})
        data.loc[2, 'ts_leave_priority_ms'] = 500  # Minimum of all four times comes first.
        frame, _ = prepare_ledger(data)
        self.assertEqual(set(frame.loc[frame['_C'], 'event_id']), {'1', '20'})

    def test_pair_order_and_scenario_are_part_of_unit_not_event_duplicate(self):
        data = pd.DataFrame([event(1, pair=('e', 'p')), event(2, pair=('p', 'e')), event(3, pair=('e', 'p'), scenario='T')])
        result = analyze_units(data)
        self.assertEqual(summary(result, 'C', 'ALL')['n_pairs'], 3)
        self.assertEqual(summary(result, 'C', 'ALL')['denominator'], 3)
        duplicate = pd.concat([data, data.iloc[[0]]], ignore_index=True)
        with self.assertRaisesRegex(ValueError, 'EVENT_KEYS'):
            analyze_units(duplicate)

    def test_source_rows_distinct_from_event_repetition_zero_and_filter_boundaries(self):
        data = pd.DataFrame([event(1, 0, nsource=2), event(2, 5, pair=('q', 'r')), event(3, 5.001, pair=('a', 'b')), event(4, .5, invalid=True)])
        result = analyze_units(data)
        a, b = summary(result, 'A'), summary(result, 'B')
        self.assertEqual(a['n_raw'], 5); self.assertEqual(a['n_unique_events_before_selection'], 4)
        self.assertEqual(a['denominator'], 3); self.assertEqual(a['numerator'], 1)
        self.assertEqual(b['denominator'], 2)

    def test_period_filters_keep_global_representative_and_missing_dates_visible(self):
        data = pd.DataFrame([event(1, -.1, date='2025-01-01'), event(2, .1, timestamp=2000, date='2025-02-01'),
                             event(3, .1, pair=('q', 'r'), date=None)])
        result = analyze_units(data)
        monthly = result['tables']['monthly_rank_sensitivity']
        feb_c = monthly.loc[monthly['unit'].eq('C') & monthly['scenario_id'].eq('S') & monthly['tau_s'].eq(1) & monthly['period_value'].eq('2025-02')].iloc[0]
        self.assertEqual(feb_c['denominator'], 0)  # February positive is not a replacement.
        leave = result['tables']['leave_one_month_rank_sensitivity']
        omit_jan_c = leave.loc[leave['unit'].eq('C') & leave['scenario_id'].eq('S') & leave['tau_s'].eq(1) & leave['period_value'].eq('2025-01')].iloc[0]
        self.assertEqual(omit_jan_c['denominator'], 1)
        self.assertEqual(result['metadata']['n_qc_valid_missing_local_date'], 1)

    def test_ranks_ties_are_exact_fraction_competition_and_ALL_not_ranked(self):
        rows = [{'scenario_id': s, 'numerator': n, 'denominator': d} for s, n, d in [('S', 1, 2), ('T', 3, 6), ('U', 3, 4), ('ZERO', 0, 0), ('ALL', 7, 12)]]
        _ranks(rows)
        self.assertEqual([(row['rank'], row['tie_count']) for row in rows[:3]], [(2, 2), (2, 2), (1, 1)])
        self.assertIsNone(rows[3]['rank']); self.assertIsNone(rows[4]['rank'])

    def test_spatial_multiplicity_reports_possible_unit_loss_without_changing_A(self):
        data = pd.DataFrame([event(1, .2), dict(event(2, .3, timestamp=2000), conflict_lon_deg=13.1)])
        result = analyze_units(data)
        row = result['tables']['pair_multiplicity'].iloc[0]
        self.assertEqual(row['n_pairs_multiple_provider_conflict_coordinates'], 1)
        self.assertEqual(row['n_events_removed_by_first_representative'], 1)
        self.assertEqual(summary(result, 'A')['denominator'], 2)
        self.assertEqual(summary(result, 'C')['denominator'], 1)

    def test_monotonic_numerators_and_numerator_never_exceeds_denominator(self):
        data = pd.DataFrame([event(i, i / 10, pair=(str(i), 'p')) for i in range(30)])
        result = analyze_units(data)
        table = result['tables']['unit_summary']
        self.assertTrue(table['numerator'].le(table['denominator']).all())
        self.assertTrue(all(group['numerator'].is_monotonic_increasing for _, group in table.groupby(['unit', 'scenario_id'])))

    def test_common_date_bootstrap_shared_indices_ties_and_valid_replicates(self):
        events = []
        for day in range(7):
            for scenario in ('S', 'T'):
                for k in range(5):
                    identifier = len(events)
                    events.append(event(identifier, .5 if k < 2 else 2, pair=(str(identifier), 'p'), scenario=scenario, date='2025-01-%02d' % (day + 1)))
        result = analyze_units(pd.DataFrame(events))
        reps = result['tables']['bootstrap_rank_replicates']
        self.assertEqual(reps['date_resample_sha256'].nunique(), 1)
        for scenario in ('S', 'T'):
            values = reps.loc[reps['unit'].eq('A') & reps['scenario_id'].eq(scenario) & reps['tau_s'].eq(1)]
            self.assertTrue(values['rank'].eq(1).all()); self.assertTrue(values['tie_count'].eq(2).all())
            self.assertTrue(values['denominator'].eq(35).all())
        draws = result['tables']['bootstrap_date_resamples']
        self.assertEqual(len(draws), 200 * 7)
        expected = np.random.default_rng(20261012).integers(0, 7, size=(200, 7))
        self.assertEqual(draws['drawn_date_index'].tolist(), expected.ravel().tolist())

    def test_zero_denominator_bootstrap_recorded_without_rank_and_missing_dates(self):
        result = analyze_units(pd.DataFrame([event(1, -.5), event(2, .5, pair=('q', 'r'), date='2025-01-02')]))
        summary_table = result['tables']['bootstrap_rank_summary']
        row = summary_table.loc[summary_table['unit'].eq('A') & summary_table['scenario_id'].eq('S') & summary_table['tau_s'].eq(1)].iloc[0]
        self.assertGreater(row['n_zero_denominator_replicates'], 0)
        self.assertEqual(row['n_valid_rank_replicates'], 0)
        self.assertEqual(row['n_zero_denominator_replicates'] + row['n_valid_fraction_replicates'], 200)
        reps = result['tables']['bootstrap_rank_replicates']
        zero = reps.loc[reps['denominator'].eq(0)]
        self.assertTrue(zero['fraction'].isna().all()); self.assertTrue(zero['rank'].isna().all())

    def test_input_order_invariance_and_original_dataframe_not_mutated(self):
        data = pd.DataFrame([event(10), event(2, .2), event(1, -.1, pair=('q', 'r'))]); original = data.copy(deep=True)
        a, b = analyze_units(data), analyze_units(data.iloc[::-1].reset_index(drop=True))
        for name in a['tables']:
            pd.testing.assert_frame_equal(a['tables'][name], b['tables'][name])
        pd.testing.assert_frame_equal(data, original)

    def test_schema_contradictions_and_budget_arguments_rejected(self):
        with self.assertRaisesRegex(ValueError, 'FIXED_B200'):
            analyze_units(pd.DataFrame([event(1)]), bootstrap_repetitions=2)
        with self.assertRaisesRegex(ValueError, 'ELIGIBILITY_STATUS'):
            analyze_units(pd.DataFrame([dict(event(1), pet_eligible='False')]))
        with self.assertRaisesRegex(ValueError, 'SIGN_STATUS'):
            analyze_units(pd.DataFrame([dict(event(1), pet_s=-1)]))
        with self.assertRaisesRegex(ValueError, 'MISSING_ORDERED_PAIR'):
            analyze_units(pd.DataFrame([dict(event(1), priority_object_id=None)]))

    def test_all_invalid_and_missing_dates_leave_zero_null_with_usable_empty_tables(self):
        data = pd.DataFrame([dict(event(1, .5, invalid=True, date=None), pet_s='not_numeric')])
        result = analyze_units(data)
        self.assertTrue(result['tables']['unit_summary']['denominator'].eq(0).all())
        self.assertTrue(result['tables']['unit_summary']['fraction'].isna().all())
        self.assertEqual(result['metadata']['bootstrap']['status'], 'NOT_APPLICABLE_NO_LOCAL_DATES')
        self.assertEqual(result['metadata']['n_input_missing_local_date'], 1)
        self.assertIn('period_type', result['tables']['monthly_rank_sensitivity'].columns)
        self.assertIn('n_pairs', result['tables']['pair_multiplicity'].columns)

    def test_unknown_QC_status_and_coercion_key_collision_do_not_silently_exclude(self):
        with self.assertRaisesRegex(ValueError, 'UNRECOGNIZED_QC_STATUS'):
            analyze_units(pd.DataFrame([dict(event(1), qc_status='UNKNOWN')]))
        with self.assertRaisesRegex(ValueError, 'EVENT_KEYS'):
            analyze_units(pd.DataFrame([dict(event(1), event_key=1), dict(event(2), event_key='1')]))


if __name__ == '__main__':
    unittest.main()
