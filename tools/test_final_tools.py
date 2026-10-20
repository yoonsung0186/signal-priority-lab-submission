"""Regression tests of launch/task/integrity tools, not of a traffic-analysis engine.
Fixture files are new and preserved under .cache/final-selftests. No user data deletion.
"""
from __future__ import annotations
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import preflight
from next_task import plan
from verify_bundle import verify_manifest

ROOT = Path(__file__).resolve().parents[1]

def task(tid, deps=None, waits=None, kind='code', priority=0):
    return {'id':tid,'requires_pass':deps or [],'waits_for_terminal':waits or [],'kind':kind,'priority':priority,'retry_limit':3}

class TestTaskPlanner(unittest.TestCase):
    def test_root_ready(self):
        self.assertEqual(plan([task('T00')])['ready'], ['T00'])
    def test_dependent_not_ready(self):
        self.assertEqual(plan([task('T00'), task('T01',['T00'])])['ready'], ['T00'])
    def test_ready_priority(self):
        r=plan([task('T00',priority=2),task('T01',priority=1)])
        self.assertEqual(r['ready'],['T01','T00'])
    def test_retry_exhaustion(self):
        r=plan([task('T00')],{'tasks':{'T00':{'state':'FAILED_RETRYABLE','attempts':3}}})
        self.assertEqual(r['projected_states']['T00'],'FAILED_FINAL')
    def test_same_error_exhaustion(self):
        r=plan([task('T00')],{'tasks':{'T00':{'state':'FAILED_RETRYABLE','attempts':2,'same_error_count':2}}})
        self.assertEqual(r['projected_states']['T00'],'FAILED_FINAL')
    def test_retry_available(self):
        r=plan([task('T00')],{'tasks':{'T00':{'state':'FAILED_RETRYABLE','attempts':1}}})
        self.assertEqual(r['ready'],['T00'])
    def test_fixed_point_block(self):
        ts=[task('T00'),task('T01',['T00']),task('T02',['T01'])]
        r=plan(ts,{'tasks':{'T00':{'state':'BLOCKED_EXTERNAL'}}})
        self.assertEqual(r['projected_states']['T02'],'BLOCKED_DEPENDENCY')
    def test_terminal_wait_not_pass(self):
        r=plan([task('T00'),task('T01',waits=['T00'])],{'tasks':{'T00':{'state':'FAILED_FINAL'}}})
        self.assertEqual(r['ready'],['T01'])
    def test_running_preserved(self):
        r=plan([task('T00')],{'tasks':{'T00':{'state':'RUNNING'}}})
        self.assertEqual(r['in_progress_or_needs_recovery'],['T00'])
        self.assertEqual(r['projected_states']['T00'],'RUNNING')
    def test_finalize_budget(self):
        ts=[task('T00'),task('T01',['T00']),task('T24',['T00'],['T01'],'finalizer')]
        r=plan(ts,{'tasks':{'T00':{'state':'PASSED'}}},True)
        self.assertEqual(r['ready'],['T24'])
    def test_cycle_rejected(self):
        with self.assertRaises(ValueError):plan([task('T00',['T01']),task('T01',['T00'])])
    def test_unknown_dependency_rejected(self):
        with self.assertRaises(ValueError):plan([task('T00',['T99'])])
    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError):plan([task('T00')],{'tasks':{'T00':{'state':'MAYBE_PASS'}}})
    def test_unknown_task_rejected(self):
        with self.assertRaises(ValueError):plan([task('T00')],{'tasks':{'T01':{}}})
    def test_no_execution_claim(self):
        r=plan([task('T00')]);self.assertFalse(r['state_written']);self.assertFalse(r['commands_executed'])

class TestFinalBoundaries(unittest.TestCase):
    def setUp(self):
        base=preflight.safe_child(preflight.verify_root(ROOT),'.cache/final-selftests')
        base.mkdir(parents=True,exist_ok=True)
        self.base=Path(tempfile.mkdtemp(prefix='case_',dir=base))
        self.root=self.base/'project';self.root.mkdir()
        (self.root/'.spl-project-root').write_text(preflight.MARKER,encoding='utf-8')
        preflight.CHECKS.clear()
    def manifest(self, rel='data.txt', content=b'hello'):
        (self.root/rel).write_bytes(content)
        (self.root/'CHECKSUMS.sha256').write_text(hashlib.sha256(content).hexdigest()+'  '+rel+'\n')
    def test_manifest_match(self):
        self.manifest();self.assertEqual(verify_manifest(self.root)['status'],'PASS')
    def test_manifest_changed(self):
        self.manifest();(self.root/'data.txt').write_text('changed')
        self.assertEqual(verify_manifest(self.root)['status'],'FAIL')
    def test_missing_member(self):
        (self.root/'CHECKSUMS.sha256').write_text('a'*64+'  absent.txt\n')
        self.assertEqual(verify_manifest(self.root)['status'],'FAIL')
    def test_traversal_rejected(self):
        (self.root/'CHECKSUMS.sha256').write_text('a'*64+'  ../outside\n')
        with self.assertRaises(ValueError):verify_manifest(self.root)
    def test_duplicate_rejected(self):
        self.manifest();p=self.root/'CHECKSUMS.sha256';p.write_text(p.read_text()*2)
        with self.assertRaises(ValueError):verify_manifest(self.root)
    def test_empty_manifest_rejected(self):
        (self.root/'CHECKSUMS.sha256').write_text('')
        with self.assertRaises(ValueError):verify_manifest(self.root)
    def test_empty_child_rejected(self):
        with self.assertRaises(ValueError):preflight.safe_child(self.root,'')
    def test_dot_child_rejected(self):
        with self.assertRaises(ValueError):preflight.safe_child(self.root,'.')
    def test_nested_own_git_rejected(self):
        (self.base/'.git').mkdir();(self.root/'.git').mkdir()
        with patch.object(preflight,'probe',return_value=(0,str(self.root))):
            self.assertFalse(preflight.inspect_git_root(self.root))
    def test_pointer_git_rejected(self):
        (self.root/'.git').write_text('gitdir: ../metadata')
        self.assertFalse(preflight.inspect_git_root(self.root))
    def test_reparse_attribute_detection(self):
        class S:st_mode=0;st_file_attributes=0x400
        with patch.object(Path,'lstat',return_value=S()):self.assertTrue(preflight.is_link_or_reparse(self.root))

if __name__=='__main__':unittest.main()
