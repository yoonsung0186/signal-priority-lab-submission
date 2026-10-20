"""PET verification guard tests; tagged mock runs are not real analysis evidence."""
import contextlib,io,json,pathlib,sys,unittest,uuid
from types import SimpleNamespace
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import pet_final as pf
import pet_delivery as pd
from final_verify import encode,digest

class PETDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/'worker_evidence'/('pet_unit_fixtures_delivery_'+uuid.uuid4().hex[:12]);self.root.mkdir(parents=True)
        self.snap={'sha256':'a'*64,'files':[]}
        self.ctx={'config_sha256':'b','lock_sha256':'c','environment_manifest_sha256':'d','environment_manifest_ref':'state/environment_manifest_r1.json','profile_config_ref':'configs/pet_interactions_v1.yaml','profile_config_sha256':'e','rights_manifest_ref':'state/rights.json','rights_manifest_sha256':'f','inputs':[{'archive':'data/input.zip','sha256':'g','bytes':1}]}
    def write(self,ref,data):
        file=self.root/ref;file.parent.mkdir(parents=True,exist_ok=True)
        with file.open('xb') as f:f.write(data)
    def stub(self,command,**kwargs):
        name=command[2];args=command[3:];raw=b'MOCK_UNIT_TEST_NOT_REAL_EXECUTION'
        e={'command':args,'exit_code':0,'source_unchanged':True,'source_tree_hash':self.snap['sha256'],'config_hash':'b','lock_hash':'c','environment_hash':'d','stdout_sha256':digest(raw),'stderr_sha256':digest(b''),'mocked_for_unit_test':True}
        for f,b in [('execution.json',encode(e)),('stdout.log',raw),('stderr.log',b'')]:self.write('state/executions/'+name+'/'+f,b)
        if '--output' in args:
            run=args[args.index('--output')+1]
            if '_synthetic_' in name:
                for f in pf.SYNTHETIC_CANONICAL_FILES:self.write(run+'/'+f,encode({'data_kind':'SYNTHETIC'}) if f.endswith('.json') else b'fixture\nmock\n')
            else:
                result={'profile_id':'PET_INTERACTIONS_V1','data_kind':'REAL','mode':'pet-analyze','input_sha256s':['g'],'pet_qc_status':'COMPLETED_PROVIDER_DERIVED_QC','pet_analysis_status':'COMPLETED_DESCRIPTIVE_BASELINE','mocked_for_unit_test':True,'ai_status':'NOT_RUN','fit_call_count':0}
                ai={'status':'NOT_RUN','fit_call_count':0,'reason':'UNIT_FIXTURE_ONLY'}
                for f in pf.MANDATORY:self.write(run+'/'+f,encode(result if f=='pet_result.json' else ai if f=='cluster_validation.json' else {}) if f.endswith('.json') else b'fixture\nmock\n')
                self.write(run+'/run_manifest.json',encode({'canonical_files':sorted(pf.MANDATORY)}))
                prov={'source_snapshot':self.snap,'config_ref':self.ctx['profile_config_ref'],'config_sha256':'e','environment_manifest_ref':self.ctx['environment_manifest_ref'],'environment_manifest_sha256':'d','lock_sha256':'c','rights_manifest_ref':self.ctx['rights_manifest_ref'],'rights_manifest_sha256':'f','inputs':self.ctx['inputs'],'status':result,'fit_call_count':0}
                self.write(run+'/provenance.json',encode(prov))
                self.write(run+'/figures/fixture.svg',b'<svg>MOCK_TEST</svg>')
        return SimpleNamespace(returncode=0,stdout='mock',stderr='')
    def scope(self):
        stack=contextlib.ExitStack();stack.enter_context(patch.object(pf,'ROOT',self.root));stack.enter_context(patch.object(pf,'snapshot',return_value=self.snap));stack.enter_context(patch.object(pf,'context',return_value=self.ctx));stack.enter_context(patch.object(pf,'verify_root',return_value=self.root));stack.enter_context(patch.object(pf.subprocess,'run',side_effect=self.stub));stack.enter_context(contextlib.redirect_stdout(io.StringIO()));return stack
    def verify(self,id='one'):
        with self.scope():pf.main(['--profile','pet-interactions-v1','--verification-id',id])
        return f'state/verifications/{id}/verification_summary.json'
    def validate(self,ref):
        with self.scope():return pf.validate_selection(ref)
    def test_two_runs_and_reuse_rejected(self):
        ref=self.verify();before=(self.root/ref).read_bytes();self.verify('two');self.validate(ref);self.assertEqual(before,(self.root/ref).read_bytes())
        with self.assertRaises(FileExistsError):self.verify()
    def test_stale_source_rejected(self):
        ref=self.verify();self.snap={'sha256':'z','files':[]}
        with self.assertRaisesRegex(ValueError,'Stale'):self.validate(ref)
    def test_changed_environment_or_input_rejected(self):
        ref=self.verify();self.ctx={**self.ctx,'environment_manifest_sha256':'new-environment'}
        with self.assertRaisesRegex(ValueError,'Stale'):self.validate(ref)
    def test_execution_log_tamper_rejected(self):
        ref=self.verify();file=self.root/'state/executions/one_pet_2/stdout.log'
        with patch.object(pathlib.Path,'read_bytes',autospec=True,side_effect=lambda p:b'changed' if p==file else original(p)):
            with self.assertRaisesRegex(ValueError,'log modified'):self.validate(ref)
    def test_missing_command_cannot_become_complete(self):
        def fail(command,**kwargs):
            value=self.stub(command,**kwargs);value.returncode=1;return value
        with self.scope(),patch.object(pf.subprocess,'run',side_effect=fail):
            with self.assertRaisesRegex(ValueError,'failed'):pf.main(['--profile','pet-interactions-v1','--verification-id','failed'])
        self.assertTrue((self.root/'state/verifications/failed/verification_failure.json').is_file())
        self.assertFalse((self.root/'state/verifications/failed/verification_summary.json').exists())
    def test_modified_figure_rejected(self):
        ref=self.verify();file=self.root/'runs/one_pet_2/figures/fixture.svg'
        with patch.object(pathlib.Path,'read_bytes',autospec=True,side_effect=lambda p: b'changed' if p==file else original(p)):
            with self.assertRaisesRegex(ValueError,'modified'):self.validate(ref)
    def test_failed_marker_rejected(self):
        ref=self.verify();self.write('state/verifications/one/verification_failure.json',b'{}')
        with self.assertRaisesRegex(ValueError,'Failed'):self.validate(ref)
    def test_incomplete_has_no_delivery_orphan(self):
        with patch.object(pd,'ROOT',self.root),patch.object(pd,'verify_root'),patch.object(pd,'validate_selection',side_effect=ValueError('incomplete')):
            with self.assertRaises(ValueError):pd.main(['--profile','pet-interactions-v1','--verification','missing','--run-id','new'])
        self.assertFalse((self.root/'delivery').exists())
    def test_existing_delivery_is_immutable(self):
        self.write('delivery/existing/keep.txt',b'keep')
        with patch.object(pd,'ROOT',self.root),patch.object(pd,'verify_root'),patch.object(pd,'validate_selection') as validate:
            with self.assertRaises(FileExistsError):pd.main(['--profile','pet-interactions-v1','--verification','missing','--run-id','existing'])
            validate.assert_not_called()
        self.assertEqual((self.root/'delivery/existing/keep.txt').read_bytes(),b'keep')
    def test_json_semantics_distinct_from_bytes(self):
        self.write('a.json',b'{"a":1,"b":2}');self.write('b.json',b'{"b":2,"a":1}\n')
        c=pf.comparison('PET','x.json',self.root/'a.json',self.root/'b.json');self.assertTrue(c['semantic_equal']);self.assertFalse(c['byte_equal']);self.assertFalse(c['equal'])
    def test_packaging_reread_bytes_must_match_verified_hash(self):
        self.write('verified.json',b'original');file=self.root/'verified.json'
        def changed(files,name,path):files[name]=b'modified after validation'
        with patch.object(pd,'add_file',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'Packaged bytes differ'):pd.add_bound({},'result.json',file,digest(b'original'),len(b'original'))
    def test_ai_attempted_failure_is_not_completed_verification(self):
        self.verify();base=self.root/'runs/one_pet_2';result=json.loads((base/'pet_result.json').read_bytes());result.update(fit_call_count=1,ai_status='AI_ATTEMPTED_FAILED')
        provenance=json.loads((base/'provenance.json').read_bytes());provenance.update(status=result,fit_call_count=1)
        changed={base/'pet_result.json':encode(result),base/'provenance.json':encode(provenance),base/'cluster_validation.json':encode({'status':'FAILED_BASELINE_PRESERVED','fit_call_count':1,'reason':'fixture failure'})}
        with self.scope(),patch.object(pathlib.Path,'read_bytes',autospec=True,side_effect=lambda p:changed[p] if p in changed else original(p)):
            with self.assertRaisesRegex(ValueError,'attempted failure'):pf.validate_provenance('runs/one_pet_2',self.snap,self.ctx)

original=pathlib.Path.read_bytes
if __name__=='__main__':unittest.main()
