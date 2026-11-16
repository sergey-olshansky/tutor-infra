"""Mocked local helper tests; no GitHub dispatch, SSH or service operations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TAG = 'lms-12345678-infra-abcdef01-run-123-1'


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='migration-helper-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bins = self.root / 'bin'
        self.bins.mkdir()
        self.log = self.root / 'calls.jsonl'
        stub = self.bins / 'stub'
        stub.write_text('#!' + sys.executable + '''
import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name;a=sys.argv[1:]
root=pathlib.Path(os.environ['FIXTURE_ROOT'])
with (root/'calls.jsonl').open('a') as out:out.write(json.dumps([name,*a])+'\\n')
mode=os.environ.get('HELPER_MODE','ok')
if name=='sleep':sys.exit(0)
if name=='ssh':
 (root/'remote.sh').write_text(sys.stdin.read());sys.exit(0)
if name=='gh' and a[:2]==['workflow','run']:
 if mode=='dispatch-failure':sys.exit(13)
 request=next(v.split('=',1)[1] for v in a if v.startswith('request_id='))
 (root/'request').write_text(request);sys.exit(0)
if name=='gh' and a[:2]==['run','list']:
 request=(root/'request').read_text()
 assert 'production-'+request+'-' in a[-1]
 if mode=='invisible':sys.exit(0)
 print('424242');sys.exit(0)
if name=='gh' and a[:2]==['run','watch']:
 sys.exit(27 if mode=='release-failure' else 0)
sys.exit(99)
''')
        stub.chmod(0o755)
        for command in ('gh', 'ssh', 'sleep'):
            (self.bins / command).symlink_to(stub)
        self.env = dict(os.environ, PATH=str(self.bins) + ':' + os.environ['PATH'],
                        FIXTURE_ROOT=str(self.root), GITHUB_ACTIONS='false')

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_helper(self, script, args=(), mode='ok', **extra_env):
        env = dict(self.env, HELPER_MODE=mode, **extra_env)
        return subprocess.run(['bash', str(ROOT / script), *args], env=env,
                              text=True, capture_output=True, timeout=15)

    def test_manual_release_dispatches_once_and_watches_correlated_run(self):
        result = self.run_helper('deploy-production.sh', [TAG])
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([c[1:3] for c in calls if c[0] == 'gh'],
                         [['workflow', 'run'], ['run', 'list'], ['run', 'watch']])
        dispatch = calls[0]
        self.assertIn('image_tag=' + TAG, dispatch)
        self.assertIn('dry_run=false', dispatch)
        self.assertEqual(dispatch[dispatch.index('--ref') + 1], 'main')
        watch = calls[-1]
        self.assertIn('424242', watch)
        self.assertIn('--exit-status', watch)
        self.assertIn('/actions/runs/424242', result.stdout)
        self.assertFalse(any(c[0] == 'ssh' for c in calls))

    def test_manual_release_propagates_failed_release(self):
        result = self.run_helper('deploy-production.sh', [TAG], mode='release-failure')
        self.assertEqual(result.returncode, 27)

    def test_dispatch_failure_is_not_retried(self):
        result = self.run_helper('deploy-production.sh', [TAG], mode='dispatch-failure')
        self.assertEqual(result.returncode, 13)
        self.assertEqual(len(self.calls()), 1)

    def test_invisible_run_is_not_redispatched(self):
        result = self.run_helper('deploy-production.sh', [TAG], mode='invisible')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn((self.root / 'request').read_text(), result.stderr)
        self.assertEqual(sum(c[:3] == ['gh', 'workflow', 'run'] for c in self.calls()), 1)
        self.assertFalse(any(c[:3] == ['gh', 'run', 'watch'] for c in self.calls()))

    def test_invalid_tag_never_dispatches(self):
        result = self.run_helper('deploy-production.sh', ['lms-unaccepted'])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_recovery_refuses_hosted_ci_before_ssh(self):
        result = self.run_helper('scripts/recover-control-local.sh', ['all'], GITHUB_ACTIONS='true')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_recovery_validates_component_before_ssh(self):
        result = self.run_helper('scripts/recover-control-local.sh', ['invalid'])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_recovery_targets_lan_and_guards_hardware(self):
        result = self.run_helper('scripts/recover-control-local.sh', ['all'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('claw@clawpi.local', self.calls()[0])
        self.assertEqual(self.calls()[0][-1], 'all')
        remote = (self.root / 'remote.sh').read_text()
        self.assertIn('$(hostname) == clawpi && $(uname -m) == aarch64', remote)
        self.assertIn('set -Eeuo pipefail', remote)


if __name__ == '__main__':
    unittest.main()
