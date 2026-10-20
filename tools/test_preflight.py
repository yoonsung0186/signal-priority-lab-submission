"""Unit tests for the basic diagnostic, not traffic-analysis tests.
Creates and preserves isolated test fixtures in project .cache/preflight-selftests.
POSIX symlink tests do not constitute native Windows junction validation.
"""
from __future__ import annotations
import importlib.util
import errno
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

p=Path(__file__).with_name('preflight.py')
s=importlib.util.spec_from_file_location('spl_preflight',p)
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)

class TestPreflight(unittest.TestCase):
    def setUp(self):
        fixture_base=m.safe_child(m.verify_root(p.parents[1]), '.cache/preflight-selftests')
        fixture_base.mkdir(parents=True, exist_ok=True)
        self.temp_path=Path(tempfile.mkdtemp(prefix='case_', dir=fixture_base))
        self.root=self.temp_path/'project'; self.root.mkdir()
        (self.root/'.spl-project-root').write_text(m.MARKER,encoding='utf-8')
        m.CHECKS.clear()
        # Case directories live inside the real project for safe preservation.
        # Hide only their outside-fixture ancestor Git markers in synthetic cases;
        # markers created in temp_path or root remain genuine filesystem checks.
        real_lexists=m.os.path.lexists
        outside_fixture_ancestors=set(self.temp_path.parents)
        def fixture_lexists(path):
            candidate=Path(path)
            if candidate.name == '.git' and candidate.parent in outside_fixture_ancestors:
                return False
            return real_lexists(path)
        git_isolation=patch.object(m.os.path,'lexists',side_effect=fixture_lexists)
        git_isolation.start()
        self.addCleanup(git_isolation.stop)
    def native_symlink(self, link, target):
        try:
            link.symlink_to(target,target_is_directory=True)
        except OSError as exc:
            if getattr(exc,'winerror',None) == 1314 or exc.errno in {errno.EPERM,errno.ENOSYS,errno.EOPNOTSUPP}:
                self.skipTest('Native symlink capability unavailable; winerror=%s errno=%s. Mocked reparse tests are separate.' % (getattr(exc,'winerror',None),exc.errno))
            raise
    def test_valid_marker(self): self.assertEqual(m.verify_root(self.root),self.root)
    def test_missing_marker(self):
        with self.assertRaises(ValueError): m.verify_root(self.temp_path)
    def test_wrong_marker(self):
        (self.root/'.spl-project-root').write_text('wrong',encoding='utf-8')
        with self.assertRaises(ValueError): m.verify_root(self.root)
    def test_relative_child(self): self.assertEqual(m.safe_child(self.root,'state/x.json'),self.root/'state/x.json')
    def test_traversal(self):
        with self.assertRaises(ValueError): m.safe_child(self.root,'../other')
    def test_absolute(self):
        with self.assertRaises(ValueError): m.safe_child(self.root,'/tmp/other')
    def test_windows_drive(self):
        with self.assertRaises(ValueError): m.safe_child(self.root,'C:/other')
    def test_windows_unc(self):
        with self.assertRaises(ValueError): m.safe_child(self.root,'\\\\host\\share')
    def test_child_symlink(self):
        self.native_symlink(self.root/'state',self.temp_path)
        with self.assertRaises(ValueError): m.safe_child(self.root,'state/escape.json')
    def test_root_symlink(self):
        link=self.temp_path/'alias'; self.native_symlink(link,self.root)
        with self.assertRaises(ValueError): m.verify_root(link)
    def test_windows_reparse_attribute_detected(self):
        attributes=SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
        with patch.object(Path,'lstat',return_value=attributes):
            self.assertTrue(m.is_link_or_reparse(self.root))
    def test_regular_directory_attribute_allowed(self):
        attributes=SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x10)
        with patch.object(Path,'lstat',return_value=attributes):
            self.assertFalse(m.is_link_or_reparse(self.root))
    def test_mocked_child_reparse_rejected(self):
        real_lstat=Path.lstat
        target=self.root/'state'
        attributes=SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
        def fixture_lstat(path):
            return attributes if path == target else real_lstat(path)
        with patch.object(Path,'lstat',fixture_lstat):
            with self.assertRaisesRegex(ValueError,'LINK_OR_REPARSE_IN_CHILD_PATH'):
                m.safe_child(self.root,'state/escape.json')
    def test_mocked_root_reparse_rejected(self):
        real_lstat=Path.lstat
        attributes=SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
        def fixture_lstat(path):
            return attributes if path == self.root else real_lstat(path)
        with patch.object(Path,'lstat',fixture_lstat):
            with self.assertRaisesRegex(ValueError,'LINK_OR_REPARSE_IN_PROJECT_ROOT'):
                m.verify_root(self.root)
    def test_mocked_marker_reparse_rejected(self):
        real_lstat=Path.lstat
        attributes=SimpleNamespace(st_mode=stat.S_IFREG,st_file_attributes=0x400)
        def fixture_lstat(path):
            return attributes if path == self.root/'.spl-project-root' else real_lstat(path)
        with patch.object(Path,'lstat',fixture_lstat):
            with self.assertRaisesRegex(ValueError,'PROJECT_MARKER_MISSING_OR_LINKED'):
                m.verify_root(self.root)
    def test_config_match_is_not_runtime_pass(self):
        (self.root/'.codex').mkdir()
        (self.root/'.codex/config.toml').write_text('model="gpt-6-astra"\nmodel_reasoning_effort="medium"\n[agents]\ndefault_subagent_model="gpt-6.1-sol"\ndefault_subagent_reasoning_effort="xhigh"\nmax_concurrent_threads_per_session=2\n')
        m.inspect_model_config(self.root)
        self.assertEqual(m.CHECKS[0]['status'],'MATCH')
        self.assertEqual(m.CHECKS[-1]['status'],'NOT_CHECKED')
    def test_missing_config_not_pass(self):
        m.inspect_model_config(self.root)
        self.assertEqual(m.CHECKS[0]['status'],'NOT_CHECKED')
    def test_missing_binary_no_auth_claim(self):
        with patch.object(m.shutil,'which',return_value=None): m.tool_version('codex',self.root)
        self.assertEqual(m.CHECKS[0]['status'],'NOT_FOUND')
    def test_version_redaction(self):
        with patch.object(m.shutil,'which',return_value='codex'),patch.object(m,'probe',return_value=(0,'codex 1.2.3 secret-value')):
            m.tool_version('codex',self.root)
        self.assertNotIn('secret-value',str(m.CHECKS))
        self.assertFalse(m.CHECKS[0]['detail']['login_verified'])
    def test_parent_git_no_binary(self):
        (self.temp_path/'.git').mkdir()
        with patch.object(m.shutil,'which',return_value=None): self.assertFalse(m.inspect_git_root(self.root))
    def test_parent_git_from_command(self):
        with patch.object(m.shutil,'which',return_value='git'),patch.object(m,'probe',return_value=(0,str(self.root.parent))):
            self.assertFalse(m.inspect_git_root(self.root))
        self.assertEqual(m.CHECKS[0]['status'],'BLOCKED')
    def test_parent_git_marker_with_binary(self):
        (self.temp_path/'.git').mkdir()
        with patch.object(m.shutil,'which',return_value='git'),patch.object(m,'probe') as probe:
            self.assertFalse(m.inspect_git_root(self.root))
            probe.assert_not_called()
        self.assertEqual(m.CHECKS[0]['status'],'BLOCKED')
    def test_git_pointer_rejected(self):
        (self.root/'.git').write_text('gitdir: ../elsewhere',encoding='utf-8')
        self.assertFalse(m.inspect_git_root(self.root))
        self.assertEqual(m.CHECKS[0]['status'],'BLOCKED')
    def test_same_git_root(self):
        (self.root/'.git').mkdir()
        with patch.object(m.shutil,'which',return_value='git'),patch.object(m,'probe',return_value=(0,str(self.root))):
            self.assertTrue(m.inspect_git_root(self.root))
        self.assertEqual(m.CHECKS[0]['status'],'PASS')
    def test_nonrepo(self):
        with patch.object(m.shutil,'which',return_value='git'),patch.object(m,'probe',return_value=(128,'')):
            self.assertTrue(m.inspect_git_root(self.root))
        self.assertEqual(m.CHECKS[0]['status'],'NO_REPOSITORY_YET')

if __name__=='__main__': unittest.main()
