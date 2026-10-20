"""Guard tests with preserved synthetic fixtures; no real analysis/model calls."""
import json,unittest,uuid
from pathlib import Path
from unittest.mock import patch
import finish_contract as contract
import finish_package as package
from spl.pet_io import file_hash

ROOT=Path(__file__).resolve().parents[1]
def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')

class FinishGuards(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/'state'/('pet_unit_fixtures_finish_guard_'+uuid.uuid4().hex);self.root.mkdir(parents=True)

    def test_execution_set_cannot_be_empty_or_missing(self):
        jobs,*_=contract.plan('synthetic_fixture')
        for records in ([],[{'name':'tools'}]):
            with self.subTest(records=records),self.assertRaisesRegex(ValueError,'execution set'):contract.validate_execution_records({'executions':records},jobs,'synthetic_fixture')

    def record(self,**updates):
        path=self.root/'state/executions/test_tools/execution.json';path.parent.mkdir(parents=True,exist_ok=True)
        for name in ('stdout.log','stderr.log'):(path.parent/name).write_text('',encoding='utf-8')
        record=dict(command=['python','test'],exit_code=0,source_unchanged=True,source_tree_hash='source',config_hash='config',environment_hash='env',lock_hash='lock',stdout_sha256=file_hash(path.parent/'stdout.log'),stderr_sha256=file_hash(path.parent/'stderr.log'))
        record.update(updates);put(path,record)
        return dict(record,name='tools',path='state/executions/test_tools/execution.json',sha256=file_hash(path))

    def test_nonzero_old_context_or_changed_record_rejected(self):
        for updates in ({'exit_code':1},{'source_unchanged':False},{'environment_hash':'old'},{'command':['other']}):
            row=self.record(**updates)
            summary={'executions':[row],'source_snapshot':{'sha256':'source'},'context':dict(config_sha256='config',environment_manifest_sha256='env',lock_sha256='lock')}
            with patch.object(contract,'ROOT',self.root),self.subTest(updates=updates),self.assertRaises(ValueError):contract.validate_execution_records(summary,[('tools',['python','test'])],'test')
        row=self.record();row['exit_code']=8
        with patch.object(contract,'ROOT',self.root),self.assertRaisesRegex(ValueError,'differs from summary'):contract.validate_execution_records({'executions':[row]},[('tools',['python','test'])],'test')

    def test_canonical_path_escape_rejected(self):
        run='runs/synthetic';path=self.root/run/'run_manifest.json'
        for bad in ('/absolute.json','../outside.json','C:/outside.json'):
            put(path,{'canonical_files':sorted(contract.MANDATORY|{bad})})
            with patch.object(contract,'ROOT',self.root),self.subTest(bad=bad),self.assertRaises(ValueError):contract.canonical(run)

    def writer_fixture(self):
        pet=['runs/pet1','runs/pet2'];evidence=['runs/evidence1','runs/evidence2'];snap={'sha256':'SYNTHETIC'}
        for p,e in zip(pet,evidence):
            put(self.root/p/'provenance.json',{'run':p})
            for name in contract.WRITER_REQUIRED:
                value={'metric':1,'input_run':p}
                if name=='metric_facts.jsonl':value['input_run_provenance_sha256']=file_hash(self.root/p/'provenance.json')
                put(self.root/e/name,value)
            put(self.root/e/'provenance.json',{'input_run':p,'input_run_provenance_sha256':file_hash(self.root/p/'provenance.json'),'source_snapshot':snap,'files':{}})
        return pet,evidence,snap

    def test_writer_json_metric_change_and_extra_file_rejected(self):
        pet,evidence,snap=self.writer_fixture()
        with patch.object(contract,'ROOT',self.root),patch.object(contract,'snapshot',return_value=snap):
            self.assertTrue(contract.compare_writer(pet,evidence))
            put(self.root/evidence[1]/'event_audit.json',{'metric':2,'input_run':pet[1]})
            with self.assertRaisesRegex(ValueError,'replay mismatch'):contract.compare_writer(pet,evidence)
            put(self.root/evidence[1]/'extra.json',{'unexpected':True})
            with self.assertRaisesRegex(ValueError,'membership drift'):contract.compare_writer(pet,evidence)

    def test_fact_provenance_hash_bound_before_narrow_normalization(self):
        pet,evidence,snap=self.writer_fixture()
        self.assertNotEqual(file_hash(self.root/pet[0]/'provenance.json'),file_hash(self.root/pet[1]/'provenance.json'))
        with patch.object(contract,'ROOT',self.root),patch.object(contract,'snapshot',return_value=snap):
            self.assertTrue(contract.compare_writer(pet,evidence))
            put(self.root/evidence[1]/'metric_facts.jsonl',{'metric':1,'input_run':pet[1],'input_run_provenance_sha256':'invented'})
            with self.assertRaisesRegex(ValueError,'input provenance hash'):contract.compare_writer(pet,evidence)

    def test_bound_copy_rejects_postverification_mutation(self):
        source=self.root/'source.json';put(source,{'metric':1});expected=(source.stat().st_size,file_hash(source));put(source,{'metric':9})
        with patch.object(package,'ROOT',self.root),patch.object(package,'BOUND',{source:expected}),self.assertRaisesRegex(ValueError,'Verified bytes changed'):package.copy(source,self.root/'target.json')
        self.assertFalse((self.root/'target.json').exists())

    def test_verified_logs_replay_and_environment_cannot_mutate_into_zip(self):
        for name in ('stdout.log','reproducibility.json','environment_manifest.json'):
            source=self.root/name;put(source,{'original':True});expected=(source.stat().st_size,file_hash(source));put(source,{'original':False})
            target=self.root/(name+'.zip')
            with patch.object(package,'ROOT',self.root),patch.object(package,'BOUND',{source:expected}),self.subTest(name=name),self.assertRaisesRegex(ValueError,'Verified bytes changed'):package.write_zip(target,{name:source})
            self.assertFalse(target.exists())

    def test_output_ancestor_reparse_rejected_before_zip(self):
        source=self.root/'safe.json';put(source,{'synthetic':True})
        target=self.root/'linked'/'out.zip'
        def mocked_reparse(path):return path==self.root/'linked'
        with patch.object(package,'ROOT',self.root),patch('preflight.is_link_or_reparse',side_effect=mocked_reparse),self.assertRaisesRegex(ValueError,'REPARSE'):package.write_zip(target,{'safe.json':source})
        self.assertFalse(target.exists())

if __name__=='__main__':unittest.main()
