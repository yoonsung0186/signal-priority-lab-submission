import pathlib,sys,unittest,unittest.mock as mock,json,uuid,zipfile,hashlib
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from package_delivery import valid_member,verify_derived_rights,write_zip
import acquire_sitpet,session_exec,preflight

class OperationsTests(unittest.TestCase):
    def test_packager_manifest_and_no_overwrite(self):
        directory=ROOT/'.cache'/'packager-tests'/uuid.uuid4().hex
        directory.mkdir(parents=True,exist_ok=False)
        target=directory/'test.zip'
        write_zip(target,{'tables/example.csv':b'value\n3\n'})
        before=target.read_bytes()
        with zipfile.ZipFile(target) as archive:
            entry=json.loads(archive.read('manifest.json'))['files'][0]
            self.assertEqual(entry['sha256'],hashlib.sha256(archive.read(entry['path'])).hexdigest())
        with self.assertRaises(FileExistsError):write_zip(target,{'changed.md':b'new'})
        self.assertEqual(before,target.read_bytes())
    def test_s12_unknown_derived_rights_block(self):
        for status in ['UNKNOWN','AMBIGUOUS','DENY',None]:
            with self.subTest(status=status),self.assertRaises(ValueError):verify_derived_rights({'rights':{'share_derived_status':status}})
        verify_derived_rights({'rights':{'share_derived_status':'ALLOW'}})
    def test_snapshot_root_file_reparse_block(self):
        import snapshot
        original=preflight.is_link_or_reparse
        with mock.patch.object(preflight,'is_link_or_reparse',side_effect=lambda p:p==ROOT/'README.md' or original(p)):
            with self.assertRaisesRegex(ValueError,'REPARSE'):snapshot.snapshot()
    def test_archive_paths(self):
        for name in ['../x','/x','C:/x','a\\x','.git/config','raw/input.csv','.venv/x','.env','nested/key.pem','raw.zip']:
            with self.subTest(name=name),self.assertRaises(ValueError):valid_member(name)
        valid_member('real_qc/table.csv')
    def test_unknown_license_blocked_before_write(self):
        with mock.patch.object(pathlib.Path,'read_text',return_value=json.dumps({'metadata':{'license':{'id':'UNKNOWN'}}})),mock.patch.object(acquire_sitpet,'verify_root'),mock.patch.object(pathlib.Path,'mkdir') as mkdir:
            with self.assertRaisesRegex(ValueError,'Rights'):acquire_sitpet.main()
            mkdir.assert_not_called()
    def test_wrong_source_host_blocked(self):
        meta={'metadata':{'license':{'id':'cc-by-4.0'}},'files':[{'key':'examples.zip','links':{'self':'https://unapproved.invalid/sample'},'size':1}]}
        with mock.patch.object(pathlib.Path,'read_text',return_value=json.dumps(meta)),mock.patch.object(acquire_sitpet,'verify_root'),mock.patch.object(pathlib.Path,'mkdir') as mkdir:
            with self.assertRaisesRegex(ValueError,'source'):acquire_sitpet.main()
            mkdir.assert_not_called()
    def test_download_limit_before_write(self):
        meta={'metadata':{'license':{'id':'cc-by-4.0'}},'files':[{'key':'examples.zip','links':{'self':'https://zenodo.org/sample'},'size':2*1024**3}]}
        with mock.patch.object(pathlib.Path,'read_text',return_value=json.dumps(meta)),mock.patch.object(acquire_sitpet,'verify_root'),mock.patch.object(pathlib.Path,'mkdir') as mkdir:
            with self.assertRaisesRegex(ValueError,'budget'):acquire_sitpet.main()
            mkdir.assert_not_called()
    def test_execution_junction_blocked(self):
        original=preflight.is_link_or_reparse
        def linked(p):return p==ROOT/'state/executions' or original(p)
        with mock.patch.object(sys,'argv',['session_exec','probe','python','--version']),mock.patch.object(preflight,'is_link_or_reparse',side_effect=linked),mock.patch.object(pathlib.Path,'mkdir') as mkdir:
            with self.assertRaisesRegex(ValueError,'REPARSE'):session_exec.main()
            mkdir.assert_not_called()
    def test_no_assert_guards(self):
        import ast
        tree=ast.parse((ROOT/'tools/acquire_sitpet.py').read_text())
        self.assertFalse(any(isinstance(n,ast.Assert) for n in ast.walk(tree)))

if __name__=='__main__':unittest.main()
