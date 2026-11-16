"""Local sender regression tests. All Telegram transport calls are mocked."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import traceback
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'send-backup-telegram.py'
SPEC = importlib.util.spec_from_file_location('backup_sender', SCRIPT)
SENDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SENDER)
STAMP = '20261116_120000'
PREFIX = STAMP + '-learn_vladastay_ru-'
SUFFIXES = ('database.sql.gz', 'files.tar', 'private-files.tar', 'site_config_backup.json')


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sender-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.files = {PREFIX + suffix: (suffix + '\n').encode() for suffix in SUFFIXES}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'PROD_SITE': 'learn.vladastay.ru'}).start()

    def archive(self, files=None, checksum_names=None, corrupt=None, extras=()):
        files = self.files if files is None else files
        checksum_names = list(files) if checksum_names is None else checksum_names
        manifest = ''.join(hashlib.sha256(files[name]).hexdigest() + '  ./' + name + '\n'
                           for name in checksum_names).encode()
        archive = self.root / 'backup.tgz'
        with tarfile.open(archive, 'w:gz') as out:
            for name, content in [*files.items(), ('SHA256SUMS', manifest), *extras]:
                if name == corrupt:
                    content += b'corruption'
                member = tarfile.TarInfo('./' + name)
                member.size = len(content)
                member.mtime = 1794830400
                out.addfile(member, io.BytesIO(content))
        return archive

    def parts(self, archive, part_size=45 * 1024 * 1024):
        folder = self.root / 'parts'
        folder.mkdir()
        return SENDER.prepare_parts(archive, folder, part_size)

    @unittest.skipUnless(hasattr(tarfile, 'data_filter'), 'sender runtime requires modern tar data filter')
    def test_canonical_four_member_archive_is_preserved(self):
        parts = self.parts(self.archive())
        self.assertEqual(len(parts), 1)
        with zipfile.ZipFile(parts[0]) as result:
            self.assertEqual(set(result.namelist()), set(self.files) | {'SHA256SUMS'})
            for name, content in self.files.items():
                self.assertEqual(result.read(name), content)

    def test_partial_backup_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self.parts(self.archive({'note.txt': b'no database or file archives'}))

    @unittest.skipUnless(hasattr(tarfile, 'data_filter'), 'sender runtime requires modern tar data filter')
    def test_incomplete_checksum_manifest_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Incomplete embedded checksum'):
            self.parts(self.archive(checksum_names=list(self.files)[:3]))

    @unittest.skipUnless(hasattr(tarfile, 'data_filter'), 'sender runtime requires modern tar data filter')
    def test_duplicate_checksum_entry_is_rejected(self):
        names = list(self.files)
        with self.assertRaisesRegex(RuntimeError, 'duplicate checksum'):
            self.parts(self.archive(checksum_names=names + names[:1]))

    @unittest.skipUnless(hasattr(tarfile, 'data_filter'), 'sender runtime requires modern tar data filter')
    def test_corrupt_member_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'checksums failed'):
            self.parts(self.archive(corrupt=next(iter(self.files))))

    def test_mixed_timestamps_are_rejected(self):
        files = dict(self.files)
        old = next(iter(files))
        files[old.replace(STAMP, '20261115_120000')] = files.pop(old)
        with self.assertRaisesRegex(RuntimeError, 'timestamps'):
            self.parts(self.archive(files))

    def test_extra_nested_member_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'archive member'):
            self.parts(self.archive(extras=[('nested/unexpected', b'extra')]))

    @unittest.skipUnless(hasattr(tarfile, 'data_filter'), 'sender runtime requires modern tar data filter')
    def test_split_parts_reassemble_without_loss(self):
        parts = self.parts(self.archive(), part_size=100)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(part.stat().st_size <= 100 for part in parts))
        self.assertEqual([part.name for part in parts],
                         [f'backup.zip.part-{i:03d}' for i in range(1, len(parts) + 1)])
        with zipfile.ZipFile(io.BytesIO(b''.join(part.read_bytes() for part in parts))) as result:
            self.assertIsNone(result.testzip())
            for name, content in self.files.items():
                self.assertEqual(result.read(name), content)

    def test_transport_timeout_redacts_token_and_does_not_retry(self):
        token = 'SYNTHETIC_BOT_TOKEN_FOR_TEST_ONLY'
        archive = self.archive()
        def timeout(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
        with patch.dict(os.environ, RELEASE_TG_BOT_TOKEN=token, RELEASE_TG_CHAT_ID='123'), \
             patch.object(sys, 'argv', ['sender', str(archive)]), \
             patch.object(SENDER, 'prepare_parts', return_value=[archive]), \
             patch.object(SENDER.subprocess, 'run', side_effect=timeout) as transport:
            try:
                SENDER.main()
            except RuntimeError:
                rendered = traceback.format_exc()
                self.assertIn('Telegram transport failed', rendered)
                self.assertNotIn(token, rendered)
            else:
                self.fail('Expected sanitized transport failure')
            self.assertEqual(transport.call_count, 1)


if __name__ == '__main__':
    unittest.main()
