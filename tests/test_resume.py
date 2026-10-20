"""R1 repair unit tests. Subprocesses are mocked; no real QC is executed here."""
import contextlib,copy,io,json,pathlib,sys,unittest,uuid
from types import SimpleNamespace
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import final_verify as fv
import package_delivery as package
from preflight import safe_child,verify_root


class ResumeRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        verify_root(ROOT)
        cls.base=safe_child(ROOT,'worker_evidence/resume_unit_fixtures_'+uuid.uuid4().hex[:12])
        cls.base.mkdir(parents=True,exist_ok=False)

    def setUp(self):
        self.root=self.base/('case_'+uuid.uuid4().hex[:12]);self.root.mkdir(exist_ok=False)
        self.write('configs/project.yaml',b'unit_test_config: true\n')
        self.write('requirements.lock',b'# UNIT_TEST_ONLY\n')
        self.write('state/environment_manifest.json',b'{"fixture_kind":"UNIT_TEST_ONLY"}')
        self.snap={'sha256':'a'*64,'files':[]}

    def write(self,relative,raw):
        path=safe_child(self.root,relative);path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as stream:stream.write(raw)
        return path

    def command_stub(self,command,**kwargs):
        """Write tagged unit fixtures in this case root, not production state/runs."""
        name=command[2];args=command[3:]
        directory=self.root/'state/executions'/name;directory.mkdir(parents=True,exist_ok=False)
        stdout=b'UNIT_TEST_MOCKED_COMMAND_NOT_REAL_VALIDATION';stderr=b''
        (directory/'stdout.log').write_bytes(stdout);(directory/'stderr.log').write_bytes(stderr)
        context=fv.verification_context(self.root)
        record={'command':args,'exit_code':0,'elapsed_s':0,'source_tree_hash':self.snap['sha256'],
                'source_unchanged':True,'config_hash':context['config_sha256'],
                'environment_hash':context['environment_manifest_sha256'],'lock_hash':context['lock_sha256'],
                'stdout_sha256':fv.digest(stdout),'stderr_sha256':fv.digest(stderr),'mocked_for_unit_test':True}
        (directory/'execution.json').write_bytes(fv.encode(record))
        if '--output' in args:
            run=args[args.index('--output')+1]
            files=fv.SYNTHETIC_CANONICAL_FILES if '_synthetic_' in name else fv.REAL_CANONICAL_FILES
            for file in files:self.write(run+'/'+file,b'{"fixture_kind":"MOCKED_UNIT_TEST_NOT_REAL_DATA","file":"'+file.encode()+b'"}')
            self.write(run+'/provenance.json',b'{"fixture_kind":"MOCKED_UNIT_TEST_NOT_REAL_DATA"}')
            self.write(run+'/figures/unit_test.svg',b'<svg xmlns="http://www.w3.org/2000/svg"><!-- UNIT_TEST_ONLY --></svg>')
        return SimpleNamespace(returncode=0,stdout=stdout.decode(),stderr='')

    def final(self,identifier,stub=None):
        with patch.object(fv,'ROOT',self.root),patch.object(fv,'verify_root',return_value=self.root),patch.object(fv,'snapshot',return_value=self.snap),patch.object(fv.subprocess,'run',side_effect=stub or self.command_stub),contextlib.redirect_stdout(io.StringIO()):
            fv.main(['--verification-id',identifier])
        return self.root/'state/verifications'/identifier

    def selection(self,identifier='selected'):
        self.final(identifier)
        return package.load_verification(self.root,'state/verifications/'+identifier)

    def validate(self,selection,snap=None):
        summary=selection['summary']
        return package.validate_verification(self.root,selection,snap or self.snap,summary['real_run'],summary['synthetic_run'])

    def package_args(self,identifier='selected',run_id='delivery_test'):
        return ['--run-id',run_id,'--real-run','runs/'+identifier+'_real_2','--synthetic-run','runs/'+identifier+'_synthetic_2','--verification-dir','state/verifications/'+identifier]

    def package_main(self,args):
        with patch.object(package,'ROOT',self.root),patch.object(package,'verify_root',return_value=self.root),patch.object(package,'snapshot',return_value=self.snap):
            return package.main(args)

    def changed_read(self,target,changed):
        original=pathlib.Path.read_bytes
        def read(path):return changed if path==target else original(path)
        return patch.object(pathlib.Path,'read_bytes',read)

    def update_repro(self,selection):
        selection['raw']['reproducibility.json']=fv.encode(selection['repro'])
        selection['summary']['reproducibility_sha256']=fv.digest(selection['raw']['reproducibility.json'])

    def test_two_distinct_verifications_preserve_legacy_and_first(self):
        legacy=self.write('state/verification_summary.json',b'LEGACY_IMMUTABLE_FIXTURE')
        first=self.final('first')
        original={file.name:file.read_bytes() for file in first.iterdir()}
        second=self.final('second')
        self.assertNotEqual(first,second)
        self.assertTrue((second/'verification_summary.json').is_file())
        self.assertEqual(original,{file.name:file.read_bytes() for file in first.iterdir()})
        self.assertEqual(legacy.read_bytes(),b'LEGACY_IMMUTABLE_FIXTURE')
        self.assertEqual(json.loads((second/'verification_summary.json').read_bytes())['verification_id'],'second')

    def test_verification_id_collision_precedes_snapshot_and_subprocess(self):
        self.final('existing')
        with patch.object(fv,'ROOT',self.root),patch.object(fv,'verify_root',return_value=self.root),patch.object(fv,'snapshot') as snapshot,patch.object(fv.subprocess,'run') as run:
            with self.assertRaises(FileExistsError):fv.main(['--verification-id','existing'])
            snapshot.assert_not_called();run.assert_not_called()

    def test_run_collision_does_not_reserve_verification(self):
        self.write('runs/collision_synthetic_1/preserved.txt',b'preserve')
        with patch.object(fv,'ROOT',self.root),patch.object(fv,'verify_root',return_value=self.root),patch.object(fv.subprocess,'run') as run:
            with self.assertRaises(FileExistsError):fv.main(['--verification-id','collision'])
            run.assert_not_called()
        self.assertFalse((self.root/'state/verifications/collision').exists())

    def test_failed_command_preserved_without_success_summary(self):
        def failing(command,**kwargs):
            self.command_stub(command,**kwargs)
            return SimpleNamespace(returncode=1,stdout='UNIT_TEST_FAILURE',stderr='')
        with self.assertRaises(RuntimeError):self.final('failed',failing)
        directory=self.root/'state/verifications/failed'
        self.assertTrue((directory/'verification_request.json').is_file())
        self.assertTrue((directory/'verification_failure.json').is_file())
        self.assertFalse((directory/'verification_summary.json').exists())
        with self.assertRaisesRegex(ValueError,'Failed'):package.load_verification(self.root,'state/verifications/failed')

    def test_all_selected_artifacts_include_figures_and_provenance(self):
        selection=self.selection()
        files=self.validate(selection)
        self.assertIn('real_qc/provenance.json',files)
        self.assertIn('synthetic/figures/unit_test.svg',files)
        self.assertEqual(len(files),14)

    def test_explicit_selection_does_not_read_legacy_metadata(self):
        self.write('state/verification_summary.json',b'INVALID_LEGACY_FIXTURE')
        selection=self.selection('chosen')
        self.assertTrue(self.validate(selection))
        self.assertEqual(selection['summary']['verification_id'],'chosen')
        with self.assertRaisesRegex(ValueError,'Explicit'):package.load_verification(self.root)

    def test_legacy_without_complete_manifest_is_rejected(self):
        self.write('state/verification_summary.json',b'{}')
        self.write('state/reproducibility.json',b'{}')
        with self.assertRaisesRegex(ValueError,'legacy verification lacks complete artifact manifest'):package.load_verification(self.root,legacy=True)

    def test_incomplete_selection_has_no_delivery_orphan(self):
        self.write('state/verifications/selected/verification_summary.json',b'{}')
        with self.assertRaisesRegex(ValueError,'Incomplete'):self.package_main(self.package_args())
        self.assertFalse((self.root/'delivery').exists())

    def test_missing_acquisition_has_no_delivery_orphan(self):
        self.selection()
        with self.assertRaises(FileNotFoundError):self.package_main(self.package_args())
        self.assertFalse((self.root/'delivery').exists())

    def test_unknown_derived_rights_has_no_delivery_orphan(self):
        self.selection();self.write('state/data_acquisition_manifest.json',b'{"rights":{"share_derived_status":"UNKNOWN"}}')
        with self.assertRaisesRegex(ValueError,'rights'):self.package_main(self.package_args())
        self.assertFalse((self.root/'delivery').exists())

    def test_delivery_collision_precedes_input_validation(self):
        preserved=self.write('delivery/existing/preserved.txt',b'IMMUTABLE')
        with patch.object(package,'ROOT',self.root),patch.object(package,'verify_root',return_value=self.root),patch.object(package,'snapshot') as snapshot:
            with self.assertRaises(FileExistsError):package.main(self.package_args(run_id='existing'))
            snapshot.assert_not_called()
        self.assertEqual(preserved.read_bytes(),b'IMMUTABLE')

    def test_stale_source_config_environment_and_run_selection(self):
        selection=self.selection()
        with self.assertRaisesRegex(ValueError,'Stale source'):self.validate(selection,{'sha256':'b'*64,'files':[]})
        for relative in ('configs/project.yaml','state/environment_manifest.json','requirements.lock'):
            with self.subTest(relative=relative),self.changed_read(self.root/relative,b'changed'),self.assertRaisesRegex(ValueError,'environment/config'):
                self.validate(selection)
        with self.assertRaisesRegex(ValueError,'Run paths'):package.validate_verification(self.root,selection,self.snap,'runs/other',selection['summary']['synthetic_run'])

    def test_artifact_tampering_is_blocked_before_delivery_creation(self):
        self.selection()
        for relative in ('runs/selected_real_2/table_inventory.csv','runs/selected_synthetic_2/result.json',
                         'runs/selected_synthetic_2/sensitivity.json','runs/selected_synthetic_2/provenance.json',
                         'runs/selected_synthetic_2/figures/unit_test.svg'):
            with self.subTest(relative=relative),self.changed_read(self.root/relative,b'changed_after_verification'),self.assertRaisesRegex(ValueError,'artifact changed'):
                self.package_main(self.package_args())
            self.assertFalse((self.root/'delivery').exists())

    def test_added_artifact_blocked(self):
        selection=self.selection()
        self.write('runs/selected_synthetic_2/unverified_extra.csv',b'UNIT_TEST_ONLY')
        with self.assertRaisesRegex(ValueError,'artifact added'):self.validate(selection)

    def test_repro_and_manifest_hash_tampering_blocked(self):
        selection=self.selection()
        for name in ('reproducibility.json','run_artifact_manifest.json'):
            altered=copy.deepcopy(selection);altered['raw'][name]+=b' '
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'hash mismatch'):self.validate(altered)

    def test_empty_or_subset_command_evidence_blocked(self):
        selection=self.selection()
        for entries in ([],selection['summary']['executions'][:1]):
            altered=copy.deepcopy(selection);altered['summary']['executions']=entries
            with self.subTest(count=len(entries)),self.assertRaisesRegex(ValueError,'command'):self.validate(altered)

    def test_empty_or_subset_canonical_evidence_blocked(self):
        selection=self.selection()
        for comparisons in ([],selection['repro']['comparisons'][:1]):
            altered=copy.deepcopy(selection);altered['repro']['comparisons']=comparisons;self.update_repro(altered)
            with self.subTest(count=len(comparisons)),self.assertRaisesRegex(ValueError,'canonical'):self.validate(altered)

    def test_execution_record_content_must_equal_summary(self):
        selection=self.selection();selection['summary']['executions'][0]['elapsed_s']=999
        with self.assertRaisesRegex(ValueError,'record and summary disagree'):self.validate(selection)

    def test_failed_marker_rejects_even_existing_success_summary(self):
        self.selection();self.write('state/verifications/selected/verification_failure.json',b'{"fixture_kind":"UNIT_TEST"}')
        with self.assertRaisesRegex(ValueError,'Failed'):package.load_verification(self.root,'state/verifications/selected')

    def test_selection_and_identifier_path_guards(self):
        for value in ('../outside','C:/outside','\\\\host\\share','state/verifications/../outside','state/verifications/x/y'):
            with self.subTest(value=value),self.assertRaises(ValueError):package.load_verification(self.root,value)
        for value in ('','../x','C:/x','a\\b','a:b','.'):
            with self.subTest(value=value),self.assertRaises(ValueError):fv.identifier(value)

    def test_mocked_reparse_guard_before_selection_reads(self):
        import preflight
        original=preflight.is_link_or_reparse
        with patch.object(preflight,'is_link_or_reparse',side_effect=lambda path:path==self.root/'state/verifications' or original(path)):
            with self.assertRaisesRegex(ValueError,'REPARSE'):package.load_verification(self.root,'state/verifications/selected')

    def test_invalid_archive_rejected_before_zip_creation(self):
        target=self.root/'forbidden.zip'
        with self.assertRaises(ValueError):package.write_zip(target,{'raw/input.csv':b'UNIT_TEST_ONLY'})
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError,'Reserved'):package.write_zip(target,{'manifest.json':b'{}'})
        self.assertFalse(target.exists())

    def test_mocked_unit_fixture_trees_not_packaged(self):
        self.write('worker_evidence/resume_unit_fixtures_example/nested/mock_execution.json',b'{"fixture_kind":"MOCKED"}')
        expected=self.write('worker_evidence/resume_repair_example/test_command.log',b'UNIT_TEST_FILTER_FIXTURE')
        self.assertEqual(package.worker_evidence_files(self.root),[expected])


if __name__=='__main__':unittest.main()
