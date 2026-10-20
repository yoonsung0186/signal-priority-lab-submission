"""Synthetic preprocessing/split tests; KMeans estimator fits are always mocked."""
from copy import deepcopy
import json,pathlib,unittest,uuid
import numpy as np
from spl.pet_ai import FEATURES,TrainingPreprocessor,chronological_split,stratified_cap,analyze_types,FitLedger,FitBudgetExhausted,count_actual_fit_calls,numeric_features
from spl.pet_run import ai_status
from spl.pet_metrics import bootstrap_descriptions,summarize

ROOT=pathlib.Path(__file__).resolve().parents[1]

def events(n=240,days=1):
    rows=[]
    for i in range(n):
        day=i*days//n
        rows.append({'event_key':'SYNTHETIC/'+str(i),'event_id':str(i),'scenario_id':'S'+str(i%2),'data_kind':'SYNTHETIC',
                     'event_date_local':'2025-01-%02d' % (day+1),'event_date_utc':'2025-01-%02d' % (day+1),'local_4h_bin':0,
                     'encroaching_object_id':'E'+str(i),'priority_object_id':'P'+str(i),'encroaching_class':'vehicle','priority_class':'pedestrian',
                     'encroaching_occupancy_s':.1+(i%50)*.04,'priority_occupancy_s':.5+(i%30)*.08,
                     'encroaching_length_m':3+(i%7)*.1,'encroaching_width_m':1.5+(i%5)*.1,'priority_length_m':.5,'priority_width_m':.5,
                     'pet_s':float(i%10)/10,'pet_eligible':True,'valid_nonnegative_pet':True,'qc_status':'VALID_NONNEGATIVE_PET','n_source_rows':1})
    return rows

class MockKMeans:
    def __init__(self,k,seed):self.k=k;self.seed=seed
    def fit(self,matrix):
        thresholds=np.quantile(matrix[:,0],np.arange(1,self.k)/self.k)
        self.labels_=np.digitize(matrix[:,0],thresholds)
        self.cluster_centers_=np.asarray([matrix[self.labels_==i].mean(axis=0) for i in range(self.k)])
        return self
    def predict(self,matrix):return np.argmin(((matrix[:,None,:]-self.cluster_centers_[None,:,:])**2).sum(axis=2),axis=1)

CONFIG={'enabled':True,'max_fit_calls':11,'min_train_events':200,'max_train_events':100000,'max_feature_missing_fraction':.2}

class PETAITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=ROOT/'worker_evidence'/('pet_unit_fixtures_ai_'+uuid.uuid4().hex[:12]);cls.base.mkdir(parents=True,exist_ok=False)

    def log(self):return self.base/(uuid.uuid4().hex+'.jsonl')
    def run_ai(self,rows=None,config=None,scope='SAMPLE_DAY',factory=MockKMeans,counter=None):
        path=self.log();result,tables=analyze_types(rows or events(),config or CONFIG,path,scope,factory,counter)
        return result,tables,path

    def test_single_day_six_mock_fits_heldout_null_and_only_allowed_features(self):
        result,tables,path=self.run_ai()
        self.assertEqual(result['status'],'FITTED_SINGLE_DAY_EXPLORATORY')
        self.assertEqual(result['fit_call_count'],6);self.assertIsNone(result['heldout_metrics']);self.assertFalse(result['generalization_claim'])
        self.assertTrue(set(result['preprocessing']['feature_names'])<=set(FEATURES))
        records=[json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(sum(row['status']=='STARTED' for row in records),6)
        self.assertEqual({row['evidence_kind'] for row in records},{'MOCKED_SYNTHETIC_UNIT_FIT'})
        self.assertEqual(sum(row['n_events'] for row in tables['cluster_profile']),240)
        self.assertEqual(ai_status(result),'EXPLORATORY_ANALYSIS_PERFORMED')

    def test_pet_identity_signal_and_cross_role_differences_not_features(self):
        rows=events();changed=deepcopy(rows)
        for row in changed:
            row.update(pet_s=999,scenario_id='changed',event_id='other',encroaching_object_id='other',signal_state='red',ts_enter_priority_ms=999)
        np.testing.assert_array_equal(numeric_features(rows),numeric_features(changed))

    def test_preprocessing_training_only(self):
        train=events(40);evaluation=deepcopy(train)
        for row in evaluation:row['encroaching_occupancy_s']=100000;row['encroaching_length_m']=99999
        preprocessor=TrainingPreprocessor().fit(train);medians=preprocessor.medians.copy();center=preprocessor.scaler.center_.copy()
        preprocessor.transform(evaluation)
        np.testing.assert_array_equal(preprocessor.medians,medians);np.testing.assert_array_equal(preprocessor.scaler.center_,center)
        self.assertLess(max(preprocessor.medians),10)

    def test_chronological_shared_objects_quarantined_both_directions(self):
        rows=events(600,10);rows[-1]['priority_object_id']=rows[0]['encroaching_object_id']
        train,test,audit=chronological_split(rows)
        self.assertEqual(audit['object_intersection_after_quarantine'],0)
        self.assertEqual(audit['quarantined_event_count'],2)
        self.assertLess(max(row['event_date_local'] for row in train),min(row['event_date_local'] for row in test))

    def test_multi_day_eleven_mock_fits_group_cv_and_no_object_leakage(self):
        result,_,_=self.run_ai(events(600,10),scope='MULTI_DAY')
        self.assertEqual(result['status'],'FITTED_CHRONOLOGICAL_HELDOUT_DESCRIPTION')
        self.assertEqual(result['fit_call_count'],11)
        self.assertEqual(result['split']['object_intersection_after_quarantine'],0)
        self.assertFalse(result['heldout_metrics']['selection_used_heldout'])
        self.assertEqual(len(result['candidate_validation']),8)

    def test_stratified_cap_deterministic_and_full_population_unmodified(self):
        rows=events(600,10);original=deepcopy(rows)
        a=stratified_cap(rows,100,20261012);b=stratified_cap(rows,100,20261012)
        self.assertEqual([row['event_key'] for row in a],[row['event_key'] for row in b]);self.assertEqual(len(a),100)
        self.assertEqual(rows,original);self.assertEqual({row['event_date_local'] for row in a},{row['event_date_local'] for row in rows})

    def test_insufficient_features_preserves_baseline_no_fit(self):
        rows=events()
        for row in rows:
            for feature in FEATURES:row[feature]=1
        result,_,path=self.run_ai(rows)
        self.assertEqual(result['status'],'NOT_RUN');self.assertEqual(result['fit_call_count'],0)
        self.assertIn('TWO_NONCONSTANT',result['reason']);self.assertEqual(path.read_bytes(),b'')

    def test_failure_and_partial_budget_are_not_not_run(self):
        class Failing(MockKMeans):
            def fit(self,matrix):raise RuntimeError('SYNTHETIC_MOCK_FAILURE')
        failed,_,path=self.run_ai(factory=Failing)
        self.assertEqual(failed['fit_call_count'],1);self.assertEqual(ai_status(failed),'AI_ATTEMPTED_FAILED')
        self.assertEqual(json.loads(path.read_text().splitlines()[-1])['status'],'FAILED')
        partial,_,_=self.run_ai(config={**CONFIG,'max_fit_calls':1})
        self.assertEqual(partial['fit_call_count'],1);self.assertEqual(ai_status(partial),'AI_INCOMPLETE')

    def test_cumulative_budget_blocks_before_any_estimator_fit(self):
        result,_,path=self.run_ai(counter=lambda:64)
        self.assertEqual(result['fit_call_count'],0);self.assertIn('CUMULATIVE_R1',result['reason']);self.assertEqual(path.read_bytes(),b'')

    def test_actual_counter_counts_started_failed_and_ignores_mock_rows(self):
        directory=self.base/uuid.uuid4().hex;(directory/'runs/fixture').mkdir(parents=True,exist_ok=False)
        records=[{'status':'STARTED','evidence_kind':'REAL_ESTIMATOR_FIT'},{'status':'FAILED','evidence_kind':'REAL_ESTIMATOR_FIT'},
                 {'status':'STARTED','evidence_kind':'MOCKED_SYNTHETIC_UNIT_FIT'}]
        (directory/'runs/fixture/fit_attempts.jsonl').write_text('\n'.join(json.dumps(row) for row in records)+'\n')
        self.assertEqual(count_actual_fit_calls(directory),1)

class PETBootstrapTests(unittest.TestCase):
    def tables(self,rows):return summarize(rows,[],{row['scenario_id']:{} for row in rows})
    def test_single_sample_day_null_inference_even_calendar_boundary(self):
        rows=events(600,10);tables=self.tables(rows)
        intervals,status=bootstrap_descriptions(rows,tables,{'scope':'SAMPLE_DAY'})
        self.assertEqual(intervals,[]);self.assertEqual(status['status'],'NOT_APPLICABLE')
        self.assertTrue(all(row['rank'] is None for row in tables['inspection_candidates']))

    def test_multi_day_seeded_conditional_intervals_and_tie_rank_range(self):
        rows=events(600,10);a=self.tables(rows);b=self.tables(rows)
        first,status=bootstrap_descriptions(rows,a,{'scope':'MULTI_DAY'});second,_=bootstrap_descriptions(rows,b,{'scope':'MULTI_DAY'})
        self.assertEqual(first,second);self.assertEqual(status['repetitions'],200)
        self.assertTrue(all(0<=row['conditional_date_cluster_interval_lower']<=row['conditional_date_cluster_interval_upper']<=1 for row in first))
        self.assertTrue(all(row['rank'] is None for row in a['inspection_candidates']))
        self.assertTrue(all(row['rank_min_across_thresholds'] is not None for row in a['inspection_candidates']))


if __name__=='__main__':unittest.main()
