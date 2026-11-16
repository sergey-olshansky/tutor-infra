"""Synthetic archive and mocked Docker tests; no server, S3 or database access."""
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'verify-backup-restore.py'
SPEC = importlib.util.spec_from_file_location('verify_restore', SCRIPT)
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)
PREFIX = '20261116_120000-learn_vladastay_ru-'
SQL = b'CREATE TABLE `fixture` (`content` longtext);\nINSERT INTO `fixture` VALUES (\'synthetic\');\n'


class InputBuffer(io.BytesIO):
    def close(self):
        self.final_bytes = self.getvalue()
        super().close()


class Child:
    def __init__(self, code=0):
        self.stdin = InputBuffer()
        self.code = code
        self.finished = False
        self.killed = False

    def wait(self):
        self.finished = True
        return self.code

    def poll(self):
        return self.code if self.finished else None

    def kill(self):
        self.killed = True
        self.finished = True


@unittest.skipUnless(hasattr(tarfile, 'data_filter'), 'isolated restore requires modern tar data filter')
class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='restore-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'backup.tgz'
        self.meminfo = self.root / 'meminfo'
        self.meminfo.write_text('MemAvailable: 2097152 kB\n')
        self.commands = []
        self.cleanup = []
        self.imports = []
        self.table_count = '87'
        self.columns = 'content\tlongtext\ninstructor_content\tlongtext'
        self.import_code = 0
        self.cancel = False
        self.child = None
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, CLAW_RESTORE_RUN='fixture-1', PROD_SITE='learn.vladastay.ru').start()
        patch.object(VERIFY, 'Path', side_effect=lambda path: self.meminfo if str(path) == '/proc/meminfo' else Path(path)).start()
        patch.object(VERIFY.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * 1024**3)).start()
        patch.object(VERIFY, 'run', side_effect=self.command).start()
        patch.object(VERIFY.subprocess, 'run', side_effect=self.cleanup_command).start()
        patch.object(VERIFY.subprocess, 'Popen', side_effect=self.import_command).start()
        patch.object(VERIFY, 'bounded_scratch', side_effect=self.scratch).start()
        self.make_archive()

    @contextlib.contextmanager
    def scratch(self, parent):
        self.assertEqual(parent, self.root)
        with tempfile.TemporaryDirectory(prefix='restore-', dir=parent) as tmp:
            yield Path(tmp)

    def inner_archive(self, name, content=b'synthetic attachment'):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as out:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mtime = 1794830400
            out.addfile(member, io.BytesIO(content))
        return data.getvalue()

    def make_archive(self, corrupt=False, bad_gzip=False, traversal=False):
        files = {
            PREFIX + 'database.sql.gz': b'invalid gzip' if bad_gzip else gzip.compress(SQL),
            PREFIX + 'files.tar': self.inner_archive('../outside.txt' if traversal else 'learn.vladastay.ru/public/files/example.txt'),
            PREFIX + 'private-files.tar': self.inner_archive('learn.vladastay.ru/private/files/private.txt'),
            PREFIX + 'site_config_backup.json': b'{"db_name":"synthetic"}',
        }
        manifest = ''.join(hashlib.sha256(content).hexdigest() + '  ./' + name + '\n'
                           for name, content in files.items()).encode()
        if corrupt:
            files[PREFIX + 'database.sql.gz'] += b'corrupt'
        files['SHA256SUMS'] = manifest
        with tarfile.open(self.archive, 'w:gz') as out:
            for name, content in files.items():
                member = tarfile.TarInfo('./' + name)
                member.size = len(content)
                member.mtime = 1794830400
                out.addfile(member, io.BytesIO(content))

    def command(self, args, **kwargs):
        self.commands.append(args)
        if args[:2] == ['docker', 'inspect']:
            self.assertEqual(args[-1], 'tutor-db-1')
            return 'sha256:' + 'a' * 64
        if args[:2] == ['docker', 'run']:
            self.assertEqual(args[args.index('--network') + 1], 'none')
            self.assertIn('--memory=384m', args)
            self.assertIn('--cpus=0.5', args)
            self.assertIn('--read-only', args)
            self.assertIn('--log-driver=none', args)
            self.assertEqual(args[args.index('--tmpfs') + 1], '/run/mysqld:rw,nosuid,nodev,noexec,size=16m')
            self.assertNotIn('--privileged', args)
            binds = [args[i + 1] for i, value in enumerate(args) if value == '-v']
            self.assertEqual(len(binds), 3)
            self.assertTrue(all(str(self.root) in value for value in binds))
            self.assertTrue(any(value.endswith(':/run/claw-client.cnf:ro') for value in binds))
            self.assertTrue(any(value.endswith(':/var/lib/mysql') for value in binds))
            temporary = next(value.rsplit(':', 1)[0] for value in binds if value.endswith(':/tmp'))
            self.assertEqual(Path(temporary).stat().st_mode & 0o7777, 0o1777)
            self.assertFalse(any('tutor_sites' in value for value in binds))
            env = Path(args[args.index('--env-file') + 1])
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            return 'synthetic-container'
        self.assertEqual(args[:3], ['docker', 'exec', 'claw-restore-check-fixture-1'])
        if args[-1] == 'SELECT 1':
            if self.cancel:
                raise KeyboardInterrupt()
            return '1'
        if 'COUNT(*)' in args[-1]:
            return self.table_count
        if 'COLUMN_NAME,DATA_TYPE' in args[-1]:
            return self.columns
        return ''

    def import_command(self, args, **kwargs):
        self.imports.append(args)
        self.assertEqual(args[:4], ['docker', 'exec', '-i', 'claw-restore-check-fixture-1'])
        self.assertEqual(args[-1], 'claw_restore_verify')
        self.assertEqual(kwargs['stdin'], subprocess.PIPE)
        self.child = Child(self.import_code)
        return self.child

    def cleanup_command(self, args, **kwargs):
        self.cleanup.append(args)
        self.assertEqual(args, ['docker', 'rm', '-f', 'claw-restore-check-fixture-1'])
        return subprocess.CompletedProcess(args, 0)

    def verify(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            VERIFY.verify(self.archive)
        return output.getvalue()

    def assert_scratch_removed(self):
        self.assertFalse(list(self.root.glob('restore-*')))

    def test_success_imports_exact_sql_and_exercises_isolated_container(self):
        output = self.verify()
        self.assertIn('ISOLATED_RESTORE_PASS tables=87 attachment_files=2', output)
        self.assertEqual(self.child.stdin.final_bytes, SQL)
        self.assertTrue(self.child.finished)
        self.assertEqual(len(self.cleanup), 1)
        self.assert_scratch_removed()

    def test_bad_checksum_blocks_container_creation(self):
        self.make_archive(corrupt=True)
        with self.assertRaisesRegex(RuntimeError, 'checksums failed'):
            self.verify()
        self.assertFalse(any(args[:2] == ['docker', 'run'] for args in self.commands))
        self.assertEqual(self.imports, [])
        self.assert_scratch_removed()

    def test_inner_attachment_traversal_blocks_container_creation(self):
        self.make_archive(traversal=True)
        with self.assertRaises(tarfile.FilterError):
            self.verify()
        self.assertFalse((self.root / 'outside.txt').exists())
        self.assertFalse(any(args[:2] == ['docker', 'run'] for args in self.commands))
        self.assert_scratch_removed()

    def test_failed_import_cleans_container_and_scratch(self):
        self.import_code = 1
        with self.assertRaisesRegex(RuntimeError, 'Database restore failed'):
            self.verify()
        self.assertEqual(len(self.cleanup), 1)
        self.assert_scratch_removed()

    def test_invalid_gzip_kills_import_child_and_cleans_container(self):
        self.make_archive(bad_gzip=True)
        with self.assertRaises(gzip.BadGzipFile):
            self.verify()
        self.assertTrue(self.child.killed)
        self.assertEqual(len(self.cleanup), 1)
        self.assert_scratch_removed()

    def test_lesson_column_type_failure_cleans_container(self):
        self.columns = 'content\ttext\ninstructor_content\tlongtext'
        with self.assertRaisesRegex(RuntimeError, 'Long Text schema'):
            self.verify()
        self.assertEqual(len(self.cleanup), 1)
        self.assert_scratch_removed()

    def test_empty_database_is_rejected(self):
        self.table_count = '0'
        with self.assertRaisesRegex(RuntimeError, 'no tables'):
            self.verify()
        self.assertEqual(len(self.cleanup), 1)
        self.assert_scratch_removed()

    def test_python_cancellation_unwinds_container_and_scratch(self):
        self.cancel = True
        with self.assertRaises(KeyboardInterrupt):
            self.verify()
        self.assertEqual(len(self.cleanup), 1)
        self.assert_scratch_removed()

    def test_low_disk_space_stops_before_docker(self):
        with patch.object(VERIFY.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(RuntimeError, 'Insufficient scratch'):
                self.verify()
        self.assertEqual(self.commands, [])

    def test_low_memory_stops_before_docker(self):
        self.meminfo.write_text('MemAvailable: 1024 kB\n')
        with self.assertRaisesRegex(RuntimeError, 'Insufficient memory'):
            self.verify()
        self.assertEqual(self.commands, [])


class BoundedScratchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='scratch-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.commands = []
        self.failure = None
        self.addCleanup(patch.stopall)
        patch.object(VERIFY.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * 1024**3)).start()
        patch.object(VERIFY, 'run', side_effect=self.command).start()

    def command(self, args, **kwargs):
        self.commands.append(args)
        if args[0] == self.failure:
            raise subprocess.CalledProcessError(1, args)
        if args[0] == 'fallocate':
            self.assertEqual(args[1:3], ['-l', str(3 * 1024**3)])
            Path(args[-1]).write_bytes(b'synthetic block device')
        return ''

    def test_scratch_allocation_mount_flags_and_normal_cleanup(self):
        with VERIFY.bounded_scratch(self.root) as scratch:
            self.assertEqual(scratch, self.root / 'restore-scratch')
            self.assertEqual(scratch.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.root / 'restore-scratch.ext4').stat().st_mode & 0o777, 0o600)
        self.assertEqual([args[0] for args in self.commands], ['fallocate', 'mkfs.ext4', 'mount', 'umount'])
        self.assertIn('nodiscard', self.commands[1])
        self.assertIn('loop,nosuid,nodev,noexec', self.commands[2])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_body_failure_unmounts_and_removes_scratch(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
            with VERIFY.bounded_scratch(self.root):
                raise RuntimeError('synthetic failure')
        self.assertEqual(self.commands[-1][0], 'umount')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_mkfs_failure_removes_unmounted_resources(self):
        self.failure = 'mkfs.ext4'
        with self.assertRaises(subprocess.CalledProcessError):
            with VERIFY.bounded_scratch(self.root):
                self.fail('failed format must not yield a filesystem')
        self.assertFalse(any(args[0] == 'mount' for args in self.commands))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_disk_margin_failure_allocates_nothing(self):
        with patch.object(VERIFY.shutil, 'disk_usage', return_value=SimpleNamespace(free=4 * 1024**3)):
            with self.assertRaisesRegex(RuntimeError, '2GiB host margin'):
                with VERIFY.bounded_scratch(self.root):
                    self.fail('insufficient space must not yield')
        self.assertEqual(self.commands, [])
        self.assertEqual(list(self.root.iterdir()), [])


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cleanup-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bins = self.root / 'bin'
        self.bins.mkdir()
        self.log = self.root / 'calls'
        stub = self.bins / 'stub'
        stub.write_text('#!' + sys.executable + "\n" + r'''import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name;a=sys.argv[1:]
with pathlib.Path(os.environ['CALL_LOG']).open('a') as f:f.write(json.dumps([name,*a])+'\n')
mode=os.environ['MODE']
if name=='docker' and a[0]=='info':sys.exit(1 if mode=='daemon-unavailable' else 0)
if name=='docker' and a[0]=='rm':sys.exit(1 if mode in ('missing','cannot-stop') else 0)
if name=='docker' and a[0]=='inspect':sys.exit(0 if mode=='cannot-stop' else 1)
if name=='mountpoint':sys.exit(0 if mode in ('ok','unmount-failure') else 1)
if name=='umount':sys.exit(1 if mode=='unmount-failure' else 0)
sys.exit(0)
''')
        stub.chmod(0o755)
        for cmd in ('docker', 'mountpoint', 'umount', 'rm', 'rmdir'):
            (self.bins / cmd).symlink_to(stub)
        self.env = dict(os.environ, PATH=str(self.bins)+':'+os.environ['PATH'], CALL_LOG=str(self.log))

    def cleanup(self, mode='ok', root='/var/tmp/claw-s3-verify-123-1', name='claw-restore-check-123-1'):
        result = subprocess.run(['bash', str(SCRIPT.with_name('cleanup-restore-check.sh')), root, name],
                                env=dict(self.env, MODE=mode), text=True, capture_output=True)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_cleanup_removes_only_named_container_and_unmounts_before_backing_image(self):
        result, calls = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0], ['docker', 'info'])
        self.assertEqual(calls[1], ['docker', 'rm', '-f', 'claw-restore-check-123-1'])
        self.assertEqual([call[0] for call in calls], ['docker', 'docker', 'mountpoint', 'umount', 'rm'])
        self.assertEqual(calls[-1][-1], '/var/tmp/claw-s3-verify-123-1/restore-scratch.ext4')

    def test_missing_container_is_idempotent(self):
        result, calls = self.cleanup(mode='missing')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(['docker', 'inspect', 'claw-restore-check-123-1'], calls)

    def test_failure_to_stop_existing_container_aborts_storage_deletion(self):
        result, calls = self.cleanup(mode='cannot-stop')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[0] in ('umount', 'rm') for call in calls))

    def test_failed_unmount_aborts_backing_image_deletion(self):
        result, calls = self.cleanup(mode='unmount-failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[0] == 'rm' for call in calls))

    def test_unavailable_docker_daemon_aborts_all_destructive_cleanup(self):
        result, calls = self.cleanup(mode='daemon-unavailable')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [['docker', 'info']])

    def test_unrelated_scratch_root_is_rejected_before_commands(self):
        result, calls = self.cleanup(root='/var/tmp/unrelated')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_active_database_name_is_rejected_before_commands(self):
        result, calls = self.cleanup(name='tutor-db-1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])


class DownloadGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='download-guard-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bins = self.root / 'bin'
        self.bins.mkdir()
        self.dest = self.root / 'download.tgz'
        self.log = self.root / 'calls'
        self.payload = b'synthetic archive bytes'
        self.key = 'lms-backup-daily-20261116T120000Z.tgz'
        stub = self.bins / 'stub'
        stub.write_text('#!' + sys.executable + '\n' + r'''import hashlib,json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name;a=sys.argv[1:]
if name=='python3':
 if 'os.statvfs' in a[1]:print(os.environ['FREE_BYTES']);sys.exit(0)
 os.execv(os.environ['REAL_PYTHON'],[os.environ['REAL_PYTHON'],*a])
with pathlib.Path(os.environ['CALL_LOG']).open('a') as f:f.write(json.dumps(a)+'\n')
payload=b'synthetic archive bytes'
if a[0]=='lsf':print('lms-backup-daily-20261116T120000Z.tgz');sys.exit(0)
if a[0]=='size':
 if os.environ.get('SIZE_FAIL')=='true':sys.exit(17)
 print(json.dumps({'bytes':int(os.environ['REMOTE_BYTES'])}));sys.exit(0)
if a[0]=='copyto':
 assert a[1].startswith('BK:'), 'test prohibits all upload operations'
 pathlib.Path(a[2]).write_bytes((hashlib.sha256(payload).hexdigest()+'  fixture\n').encode() if a[1].endswith('.sha256') else payload)
 sys.exit(0)
sys.exit(99)
''')
        stub.chmod(0o755)
        for cmd in ('rclone', 'python3'):
            (self.bins / cmd).symlink_to(stub)
        self.env = dict(os.environ, PATH=str(self.bins)+':'+os.environ['PATH'],
                        REAL_PYTHON=sys.executable, CALL_LOG=str(self.log), S3_BUCKET='synthetic',
                        S3_ACCESS_KEY_ID='synthetic', S3_SECRET_ACCESS_KEY='synthetic',
                        S3_MIN_FREE_BYTES='1000', FREE_BYTES='10000', REMOTE_BYTES=str(len(self.payload)))

    def download(self, **extra):
        result = subprocess.run(['bash', str(SCRIPT.with_name('s3-backup.sh')), 'download', 'latest', str(self.dest)],
                                env=dict(self.env, **extra), text=True, capture_output=True, timeout=15)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_download_succeeds_with_margin_and_verified_payload(self):
        result, calls = self.download()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertEqual([call[0] for call in calls], ['lsf', 'size', 'copyto', 'copyto'])
        self.assertIn('Verified sha256', result.stdout)

    def test_insufficient_margin_rejects_before_first_download_byte(self):
        result, calls = self.download(FREE_BYTES=str(1000 + len(self.payload) - 1))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Insufficient space', result.stderr)
        self.assertFalse(self.dest.exists())
        self.assertFalse(any(call[0] == 'copyto' for call in calls))

    def test_failed_remote_size_query_does_not_download(self):
        result, calls = self.download(SIZE_FAIL='true')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.dest.exists())
        self.assertFalse(any(call[0] == 'copyto' for call in calls))

    def test_invalid_margin_does_not_download(self):
        result, calls = self.download(S3_MIN_FREE_BYTES='invalid')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.dest.exists())
        self.assertFalse(any(call[0] in ('copyto', 'size') for call in calls))

    def test_zero_sized_s3_object_does_not_download(self):
        result, calls = self.download(REMOTE_BYTES='0')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.dest.exists())
        self.assertFalse(any(call[0] == 'copyto' for call in calls))

    def test_optional_guard_does_not_change_existing_download_contract(self):
        result, calls = self.download(S3_MIN_FREE_BYTES='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == 'size' for call in calls))
        self.assertEqual(self.dest.read_bytes(), self.payload)


if __name__ == '__main__':
    unittest.main()
