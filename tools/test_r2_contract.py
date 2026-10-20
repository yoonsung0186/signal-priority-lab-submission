"""Synthetic regression fixtures for no-fit evidence and package binding."""
import json
from pathlib import Path
import unittest
import uuid
import sys
from r2_package import validate_verification, REQUIRED_R2, BOUND, bind, archive, copy_bound
from r2_postprocess import source_snapshot
import hashlib
import zipfile
from r2_postprocess import ROOT, save, deny_fit
from spl.pet_io import file_hash


class R2ContractTests(unittest.TestCase):
    def setUp(self):BOUND.clear()

    def fixture(self, omit=None, wrong_context=False):
        folder=ROOT/'worker_evidence'/('r2_contract_'+uuid.uuid4().hex)
        folder.mkdir(parents=True);verification=folder/'verification';verification.mkdir()
        source=source_snapshot();config=json.loads((ROOT/'configs/r2_postprocess.json').read_bytes())
        config_hash=file_hash(ROOT/'configs/r2_postprocess.json')
        save(verification/'source_snapshot.json',source)
        runs=[]
        for name in ('first','second'):
            run=folder/name;run.mkdir()
            for ref in sorted(REQUIRED_R2-{'artifact_manifest.json','reproducibility_context.json','numeric_checks.json'}):
                if ref==omit:continue
                target=run/ref;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'SYNTHETIC_TEST_FIXTURE\n')
            save(run/'reproducibility_context.json',{'source':source,'config':config,'config_sha256':config_hash,
                 'reuse_manifest_sha256':file_hash(ROOT/config['reuse_manifest']),'postprocess_source_commit':'WRONG' if wrong_context else 'SYNTHETIC',
                 'actual_fit_before':52,'actual_fit_after':52,'actual_new_fits':0,'baseline_full_replay_in_r2':False,'independent_accuracy':None,'manual_ground_truth':'NOT_PERFORMED'})
            save(run/'numeric_checks.json',{'status':'MATCH','baseline_unchanged':True,'A_baseline_counts_and_thresholds':True,'signal_condition_partition_matches_A':True})
            save(run/'artifact_manifest.json',{'files':[{'path':p.relative_to(run).as_posix(),'sha256':file_hash(p),'bytes':p.stat().st_size} for p in sorted(run.rglob('*')) if p.is_file()]})
            runs.append(run.relative_to(ROOT).as_posix())
        save(verification/'environment.json',{'fixture':'SYNTHETIC','r2_lock_sha256':source['r2_lock']['sha256']})
        records=[]
        expected={'tools':[sys.executable,'-m','unittest','discover','-s','tools','-p','test_*.py','-v'],
                  'implementation':[sys.executable,'-m','unittest','discover','-s','tests','-v'],
                  'optimized':[sys.executable,'-O','-m','unittest','discover','-s','tests','-p','test_ops.py','-v'],
                  'preflight':[sys.executable,'tools/preflight.py','--root','.']}
        expected.update({'postprocess_'+str(i+1):[sys.executable,'tools/r2_postprocess.py','--out',run] for i,run in enumerate(runs)})
        for name in ('tools','implementation','optimized','preflight','postprocess_1','postprocess_2'):
            for stream in ('stdout','stderr'):(verification/(name+'.'+stream+'.log')).write_bytes(b'SYNTHETIC_TEST_FIXTURE')
            r={'name':name,'command':expected[name],'exit_code':0,'source_unchanged':True,'source_tree_hash':source['sha256'],'config_sha256':config_hash,
               'environment_sha256':file_hash(verification/'environment.json'),
               **{s+'_sha256':file_hash(verification/(name+'.'+s+'.log')) for s in ('stdout','stderr')}}
            records.append(r);save(verification/(name+'.execution.json'),r)
        save(verification/'verification_request.json',{'commands':[[r['name'],r['command']] for r in records],'run_paths':runs,'source_tree_hash':source['sha256'],'actual_fit_before':52,'new_fit_budget':0,'baseline_full_replay_in_r2':False})
        repro={'status':'MATCH','run_paths':runs,'files':[{'path':p.relative_to(ROOT/runs[0]).as_posix(),'sha256':file_hash(p),'bytes':p.stat().st_size,'byte_equal':True} for p in sorted((ROOT/runs[0]).rglob('*')) if p.is_file()]}
        save(verification/'reproducibility.json',repro)
        v={'source_commit':'SYNTHETIC','source_tree_hash':source['sha256'],'config_sha256':config_hash,'executions':records,'reproducibility_sha256':file_hash(verification/'reproducibility.json'),'selected_run':runs[-1]}
        return verification/'verification_summary.json',v,ROOT/runs[-1]

    def test_exact_two_replay_artifacts_accepted(self):
        vp,v,_=self.fixture();self.assertEqual(validate_verification(vp,v)['status'],'MATCH')

    def test_changed_output_and_locally_rewritten_manifest_rejected(self):
        vp,v,run=self.fixture();(run/'unit_summary.csv').write_bytes(b'n,N\n2,2\n')
        (run/'artifact_manifest.json').write_text(json.dumps({'files':[{'path':'unit_summary.csv','sha256':file_hash(run/'unit_summary.csv')}]}))
        with self.assertRaisesRegex(ValueError,'bytes changed'):validate_verification(vp,v)

    def test_unlisted_extra_output_rejected(self):
        vp,v,run=self.fixture();(run/'extra.csv').write_bytes(b'SYNTHETIC')
        with self.assertRaisesRegex(ValueError,'membership changed'):validate_verification(vp,v)

    def test_failed_execution_marker_rejected(self):
        vp,v,_=self.fixture();save(vp.parent/'failure.json',{'fixture':'SYNTHETIC'})
        with self.assertRaisesRegex(ValueError,'failure marker'):validate_verification(vp,v)

    def test_actual_fit_entry_is_blocked(self):
        with self.assertRaisesRegex(RuntimeError,'zero new model fits'):deny_fit(None)

    def test_named_but_wrong_command_rejected(self):
        vp,v,_=self.fixture();v['executions'][0]['command']=[sys.executable,'-c','pass']
        (vp.parent/'tools.execution.json').write_text(json.dumps(v['executions'][0]))
        with self.assertRaisesRegex(ValueError,'command mismatch'):validate_verification(vp,v)


    def test_missing_required_artifact_rejected(self):
        vp,v,_=self.fixture(omit='signal_state_transitions.csv')
        with self.assertRaisesRegex(ValueError,'required R2'):validate_verification(vp,v)

    def test_equal_but_wrong_replay_context_rejected(self):
        vp,v,_=self.fixture(wrong_context=True)
        with self.assertRaisesRegex(ValueError,'context binding'):validate_verification(vp,v)

    def test_postvalidation_output_mutation_rejected_by_archive(self):
        vp,v,run=self.fixture();validate_verification(vp,v)
        path=run/'unit_summary.csv';path.write_bytes(b'CHANGED_AFTER_VALIDATION')
        with self.assertRaisesRegex(ValueError,'Bound bytes changed'):archive(run.parent/'attempt.zip',{'table.csv':path})

    def test_postvalidation_output_mutation_rejected_by_writer_copy(self):
        vp,v,run=self.fixture();validate_verification(vp,v)
        path=run/'unit_summary.csv';path.write_bytes(b'CHANGED_AFTER_VALIDATION')
        with self.assertRaisesRegex(ValueError,'Bound bytes changed'):copy_bound(path,run.parent/'attempt.csv')

    def test_expected_digest_cannot_be_rebound(self):
        vp,v,run=self.fixture();validate_verification(vp,v);path=run/'unit_summary.csv'
        with self.assertRaisesRegex(ValueError,'Conflicting'):bind(path,{'bytes':path.stat().st_size,'sha256':'0'*64})

    def test_bound_writer_and_zip_preserve_same_bytes(self):
        vp,v,run=self.fixture();validate_verification(vp,v);path=run/'unit_summary.csv';target=run.parent/'writer.csv'
        expected=BOUND[path].copy();copy_bound(path,target);archive(run.parent/'valid.zip',{'table.csv':target})
        with zipfile.ZipFile(run.parent/'valid.zip') as z:self.assertEqual(hashlib.sha256(z.read('table.csv')).hexdigest(),expected['sha256'])
        self.assertEqual(BOUND[target],expected)


if __name__=='__main__':unittest.main()
