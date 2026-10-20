import unittest
import pandas as pd
from spl.r2_reporting import candidate_table


class R2ReportingTests(unittest.TestCase):
    def tables(self,zero=False):
        base={'scenario_id':'S','unit':'A','tau_s':1.,'rank':None if zero else 1,'numerator':0 if zero else 1,
              'denominator':0 if zero else 2,'fraction':None if zero else .5}
        period=pd.DataFrame([{**base,'rank':None if zero else 2},{**base,'unit':'B','rank':99}])
        return {'unit_summary':pd.DataFrame([base]),'monthly_rank_sensitivity':period,
                'leave_one_month_rank_sensitivity':period,'bootstrap_rank_summary':pd.DataFrame([
                    {'scenario_id':'S','unit':'A','tau_s':1.,'rank_min':None if zero else 2,'rank_max':None if zero else 2}]),
                'signal_event_changes':pd.DataFrame(columns=['scenario_id','population','pair_state_change_rate']),
                'pair_multiplicity':pd.DataFrame(columns=['scenario_id','n_pairs_multiple_provider_conflict_coordinates'])}

    def test_singleton_period_rank_different_from_baseline_is_unstable(self):
        row=candidate_table(self.tables(),{'S':'Fixture'}).iloc[0]
        self.assertEqual(row.candidate_status,'ADDITIONAL_OBSERVATION_REQUIRED_UNSTABLE')
        self.assertEqual(row.monthly_rank_max,2)  # B rank must not contaminate period A sensitivity.

    def test_no_support_is_not_stable_and_zero_denominator_remains_null(self):
        row=candidate_table(self.tables(True),{'S':'Fixture'}).iloc[0]
        self.assertEqual(row.candidate_status,'INSUFFICIENT_SUPPORT_ADDITIONAL_OBSERVATION_REQUIRED')
        self.assertTrue(pd.isna(row.original_A_fraction))
        self.assertTrue(pd.isna(row.original_A_rank))


if __name__=='__main__':unittest.main()
