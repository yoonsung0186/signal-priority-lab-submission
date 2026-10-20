"""Synthetic checks of completed-R2 provenance binding; no real replay or fit."""
import hashlib
import json
import csv
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid
import r2_signal_quality as target


class ReusedContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=target.ROOT/'worker_evidence'/('quality_context_fixture_'+uuid.uuid4().hex)
        cls.root.mkdir(parents=True)
        for name,data in [('reuse.json',b'{}'),('requirements-r2.lock',b'synthetic-lock\n'),('original.py',b'# preserved synthetic source\n')]:
            (cls.root/name).write_bytes(data)
        cls.old={'reuse_manifest':'reuse.json'}
        cls.config={'r2_run':'run','r2_source_commit':'synthetic-source-commit'}
        (cls.root/'run').mkdir()
        context={'config':cls.old,'config_sha256':'synthetic-config-hash','postprocess_source_commit':cls.config['r2_source_commit'],
                 'reuse_manifest_sha256':target.file_hash(cls.root/'reuse.json'),
                 'source':{'r2_lock':{'sha256':target.file_hash(cls.root/'requirements-r2.lock')},
                           'files':[{'path':'original.py','bytes':(cls.root/'original.py').stat().st_size,'sha256':target.file_hash(cls.root/'original.py')}]}}
        (cls.root/'run/reproducibility_context.json').write_text(json.dumps(context),encoding='utf-8')

    def check(self,config=None,old=None,digest='synthetic-config-hash'):
        with patch.object(target,'ROOT',self.root):
            target.check_reused_context(config or self.config,old or self.old,digest)

    def test_exact_context_accepted(self):self.check()
    def test_config_hash_mismatch_rejected(self):
        with self.assertRaises(ValueError):self.check(digest='changed')
    def test_config_content_mismatch_rejected(self):
        with self.assertRaises(ValueError):self.check(old={**self.old,'extra':True})
    def test_source_commit_mismatch_rejected(self):
        with self.assertRaises(ValueError):self.check(config={**self.config,'r2_source_commit':'different'})

    def changed_file(self,name):
        original=target.file_hash
        def digest(path):return 'changed' if Path(path).name==name else original(path)
        with patch.object(target,'file_hash',side_effect=digest):
            with self.assertRaises(ValueError):self.check()

    def test_reuse_manifest_mismatch_rejected(self):self.changed_file('reuse.json')
    def test_lock_mismatch_rejected(self):self.changed_file('requirements-r2.lock')
    def test_original_source_mismatch_rejected(self):self.changed_file('original.py')

    def test_overlay_preserves_literal_decimal_strings(self):
        import pandas as pd
        original=self.root/('literal_'+uuid.uuid4().hex+'.csv')
        original.write_text('scenario_id,tau_s,value\nX,1,0.10000000000000001\n',encoding='utf-8')
        table=pd.read_csv(original);table['interpretable_n']=0
        out=self.root/('overlay_'+uuid.uuid4().hex+'.csv')
        target.write_overlay_csv(out,table,original)
        with out.open(newline='') as stream:row=next(csv.DictReader(stream))
        self.assertEqual(row['value'],'0.10000000000000001')
        self.assertEqual(row['tau_s'],'1')
        self.assertEqual(row['interpretable_n'],'0')


if __name__=='__main__':unittest.main()
