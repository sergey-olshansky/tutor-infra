"""Filesystem regression tests for the production backup's embedded retention."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'backup-production.sh'
SOURCE = SCRIPT.read_text()
CODE = SOURCE.split("<<'PY_LOCAL_RETENTION'\n", 1)[1].split('\nPY_LOCAL_RETENTION\n', 1)[0]
SITE = 'learn.vladastay.ru'
LABEL = 'learn_vladastay_ru'
OLD = '20261104_230000'
CURRENT = '20261105_081950'
SUFFIXES = ('database.sql.gz', 'files.tar', 'private-files.tar', 'site_config_backup.json')


class LocalRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_set(self, stamp, suffixes=SUFFIXES, label=LABEL):
        names = [f'{stamp}-{label}-{suffix}' for suffix in suffixes]
        for name in names:
            (self.root / name).write_bytes(b'backup fixture\n')
        return names

    def run_retention(self, names):
        return subprocess.run([sys.executable, '-', str(self.root), SITE, *names],
                              input=CODE, text=True, capture_output=True)

    def test_removes_older_complete_and_partial_sets(self):
        self.make_set(OLD)
        partial = self.make_set('20261104_235959', SUFFIXES[:1])
        current = self.make_set(CURRENT)
        result = self.run_retention(current)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(p.name for p in self.root.iterdir()), set(current))
        self.assertFalse((self.root / partial[0]).exists())

    def test_same_day_uses_full_timestamp(self):
        old = self.make_set('20261105_010000')
        current = self.make_set(CURRENT)
        self.assertEqual(self.run_retention(current).returncode, 0)
        self.assertTrue(all(not (self.root / name).exists() for name in old))

    def test_foreign_files_and_other_site_preserved(self):
        other = self.make_set(OLD, label='another_site')
        (self.root / 'keep-me.txt').write_text('keep')
        current = self.make_set(CURRENT)
        self.assertEqual(self.run_retention(current).returncode, 0)
        self.assertTrue(all((self.root / name).exists() for name in other))
        self.assertTrue((self.root / 'keep-me.txt').exists())

    def test_newer_sets_preserved(self):
        newer = self.make_set('20261106_000001')
        partial = self.make_set('20261106_000002', SUFFIXES[:1])
        current = self.make_set(CURRENT)
        self.assertEqual(self.run_retention(current).returncode, 0)
        self.assertTrue(all((self.root / name).exists() for name in newer + partial))

    def test_symlinks_and_their_target_preserved(self):
        target = self.root / 'keep-me.txt'
        target.write_text('keep')
        link = self.root / f'{OLD}-{LABEL}-files.tar'
        link.symlink_to(target)
        current = self.make_set(CURRENT)
        self.assertEqual(self.run_retention(current).returncode, 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), 'keep')

    def assert_rejected_without_deletion(self, names):
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        result = self.run_retention(names)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})

    def test_mixed_timestamps_fail_before_deletion(self):
        old = self.make_set(OLD)
        current = self.make_set(CURRENT)
        self.assert_rejected_without_deletion([old[0], *current[1:]])

    def test_duplicate_member_rejected(self):
        self.make_set(OLD)
        current = self.make_set(CURRENT)
        self.assert_rejected_without_deletion([current[0], current[0], *current[2:]])

    def test_missing_member_rejected(self):
        self.make_set(OLD)
        current = self.make_set(CURRENT)
        (self.root / current[0]).unlink()
        self.assert_rejected_without_deletion(current)

    def test_empty_member_rejected(self):
        self.make_set(OLD)
        current = self.make_set(CURRENT)
        (self.root / current[0]).write_bytes(b'')
        self.assert_rejected_without_deletion(current)

    def test_noncanonical_member_rejected(self):
        self.make_set(OLD)
        current = self.make_set(CURRENT)
        self.assert_rejected_without_deletion(['../outside', *current[1:]])

    def test_compressed_file_archives_supported(self):
        self.make_set(OLD)
        current = self.make_set(CURRENT, ('database.sql.gz', 'files.tar.gz',
                                          'private-files.tar.gz', 'site_config_backup.json'))
        self.assertEqual(self.run_retention(current).returncode, 0)
        self.assertEqual({p.name for p in self.root.iterdir()}, set(current))

    def test_idempotent(self):
        current = self.make_set(CURRENT)
        self.assertEqual(self.run_retention(current).returncode, 0)
        result = self.run_retention(current)
        self.assertEqual(result.returncode, 0)
        self.assertIn('removed 0 older files', result.stdout)

    def integrated_backup(self, control_failure):
        # Real Bash, tar and embedded Python; only external services/OS CLI
        # differences are replaced. This test does not certify flock semantics.
        old = self.make_set(OLD)
        current = self.make_set(CURRENT)
        bins = self.root / 'bin'
        bins.mkdir()
        stub = bins / 'stub'
        stub.write_text('''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
name=pathlib.Path(sys.argv[0]).name;a=sys.argv[1:]
root=pathlib.Path(os.environ['FIXTURE_ROOT'])
if name in ['find','flock']:sys.exit(0)
if name=='stat':print(pathlib.Path(a[-1]).stat().st_size);sys.exit(0)
if name=='ssh':
 sys.stdin.buffer.read()
 sys.exit(17 if os.environ['CONTROL_FAILURE']=='1' else 0)
if name=='docker':
 if a[0]=='compose':
  print('mock-cid' if 'ps' in a else 'bench backup complete');sys.exit(0)
 if a[0]=='cp':
  shutil.copyfile(root/pathlib.Path(a[1]).name,pathlib.Path(a[2])/pathlib.Path(a[1]).name);sys.exit(0)
 if a[0]=='exec' and 'python3' in a:
  (root/'retention-executed').touch()
  i=a.index('python3');args=a[i+1:];args[1]=str(root)
  os.execv(sys.executable,[sys.executable,*args])
 if a[0]=='exec':
  print('\\n'.join(json.loads(os.environ['CURRENT_NAMES'])));sys.exit(0)
sys.exit(2)
''')
        stub.chmod(0o755)
        for name in ['docker', 'ssh', 'find', 'stat', 'flock']:
            (bins / name).symlink_to(stub)
        script = self.root / 'backup-local-paths.sh'
        script.write_text(SOURCE.replace('/tmp', str(self.root)))
        env = dict(os.environ, PATH=str(bins)+':'+os.environ['PATH'],
                   FIXTURE_ROOT=str(self.root), CURRENT_NAMES=json.dumps(current),
                   CONTROL_FAILURE='1' if control_failure else '0',
                   TUTOR_COMPOSE_DIR=str(self.root), TUTOR_SITE=SITE,
                   TUTOR_BACKUP_LOCK_FILE=str(self.root/'server.lock'))
        result = subprocess.run(['bash', str(script), 'control.invalid', 'daily'],
                                env=env, text=True, capture_output=True, timeout=20)
        self.assertFalse(list(self.root.glob('lms-backup-staging.*')))
        if control_failure:
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(all((self.root / name).exists() for name in old + current))
            self.assertFalse((self.root/'retention-executed').exists())
            self.assertNotIn('BACKUP_FILE=', result.stdout)
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(all(not (self.root / name).exists() for name in old))
            self.assertTrue(all((self.root / name).exists() for name in current))
            self.assertTrue((self.root/'retention-executed').exists())
            self.assertIn('BACKUP_FILE=', result.stdout)
            self.assertEqual(len(list(self.root.glob('lms-backup-daily-*.tgz'))), 1)

    def test_full_script_prunes_after_successful_control_delivery(self):
        self.integrated_backup(False)

    def test_full_script_control_failure_does_not_run_retention(self):
        self.integrated_backup(True)


if __name__ == '__main__':
    unittest.main()
