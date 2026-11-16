#!/usr/bin/env python3
"""Restore an existing S3 archive in a disposable, isolated preprod MariaDB instance."""
import gzip
from contextlib import contextmanager
import importlib.util
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()



@contextmanager
def bounded_scratch(parent):
    budget = 3 * 1024**3
    if shutil.disk_usage(parent).free < budget + 2 * 1024**3:
        raise RuntimeError('Restore needs a reserved 3GiB scratch filesystem and 2GiB host margin')
    image = parent / 'restore-scratch.ext4'
    root = parent / 'restore-scratch'
    if image.exists() or root.exists():
        raise RuntimeError('Scratch resources already exist; cleanup required')
    root.mkdir(mode=0o700)
    mounted = False
    try:
        run(['fallocate', '-l', str(budget), str(image)])
        image.chmod(0o600)
        run(['mkfs.ext4', '-q', '-F', '-m', '0', '-E', 'nodiscard', str(image)])
        run(['mount', '-o', 'loop,nosuid,nodev,noexec', str(image), str(root)])
        mounted = True
        root.chmod(0o700)
        yield root
    finally:
        if mounted:
            run(['umount', str(root)])
        image.unlink(missing_ok=True)
        root.rmdir()


def verify(archive: Path):
    if archive.stat().st_size < 1:
        raise RuntimeError('Empty archive')
    if shutil.disk_usage('/var/tmp').free < max(3 * 1024**3, archive.stat().st_size * 5):
        raise RuntimeError('Insufficient scratch disk space; active preprod is unchanged')
    available = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
    if available < 768 * 1024:
        raise RuntimeError('Insufficient memory for isolated restore; active preprod is unchanged')
    spec = importlib.util.spec_from_file_location('backup_archive', Path(__file__).with_name('send-backup-telegram.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    image = run(['docker', 'inspect', '--format', '{{.Image}}', 'tutor-db-1'])
    if not image.startswith('sha256:'):
        raise RuntimeError('Expected immutable local preprod database image')
    run_id = os.environ.get('CLAW_RESTORE_RUN', secrets.token_hex(6))
    if not re.fullmatch(r'[a-z0-9-]{1,64}', run_id):
        raise RuntimeError('Invalid restore run identifier')
    name = 'claw-restore-check-' + run_id
    with bounded_scratch(archive.parent) as root:
        extracted = helper.validate_and_extract(archive, root)
        database = list(extracted.glob('*-database.sql.gz'))
        if len(database) != 1:
            raise RuntimeError('Expected one database dump')
        restored_files = 0
        for i, files in enumerate(p for p in extracted.iterdir() if '-files.tar' in p.name):
            destination = root / f'attachments-{i}'
            destination.mkdir()
            with tarfile.open(files) as inner:
                inner.extractall(destination, filter='data')
            restored_files += sum(p.is_file() for p in destination.rglob('*'))
        password = secrets.token_hex(32)
        env = root / 'database.env'; env.write_text('MARIADB_ROOT_PASSWORD=' + password + '\n'); env.chmod(0o600)
        client = root / 'client.cnf'; client.write_text('[client]\nuser=root\npassword=' + password + '\n'); client.chmod(0o600)
        data = root / 'db'; data.mkdir()
        server_tmp = root / 'server-tmp'; server_tmp.mkdir(); server_tmp.chmod(0o1777)
        sql = ['docker', 'exec', name, 'mariadb', '--defaults-extra-file=/run/claw-client.cnf', '--batch', '--skip-column-names']
        try:
            run(['docker', 'run', '-d', '--name', name, '--network', 'none', '--memory=384m', '--memory-swap=512m', '--cpus=0.5',
                 '--read-only', '--log-driver=none', '--tmpfs', '/run/mysqld:rw,nosuid,nodev,noexec,size=16m',
                 '--env-file', str(env), '-v', f'{client}:/run/claw-client.cnf:ro', '-v', f'{data}:/var/lib/mysql', '-v', f'{server_tmp}:/tmp', image,
                 '--innodb-buffer-pool-size=32M', '--max-connections=5', '--performance-schema=OFF'])
            deadline = time.monotonic() + 90
            while True:
                try:
                    run(sql + ['--execute', 'SELECT 1'])
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() > deadline:
                        raise RuntimeError('Disposable database did not become ready') from None
                    time.sleep(1)
            run(sql + ['--execute', 'CREATE DATABASE claw_restore_verify CHARACTER SET utf8mb4'])
            with gzip.open(database[0], 'rb') as source, tempfile.TemporaryFile(dir=root) as errors:
                child = subprocess.Popen(sql[:2] + ['-i'] + sql[2:] + ['claw_restore_verify'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
                try:
                    shutil.copyfileobj(source, child.stdin, 1024 * 1024)
                    child.stdin.close()
                    if child.wait() != 0:
                        raise RuntimeError('Database restore failed; production/preprod sites are unchanged')
                finally:
                    if child.poll() is None:
                        child.kill(); child.wait()
            tables = int(run(sql + ['--execute', "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='claw_restore_verify'"]))
            if tables < 1:
                raise RuntimeError('Restored database has no tables')
            columns = run(sql + ['--execute', "SELECT COLUMN_NAME,DATA_TYPE FROM information_schema.columns WHERE table_schema='claw_restore_verify' AND TABLE_NAME='tabCourse Lesson' AND COLUMN_NAME IN ('content','instructor_content') ORDER BY COLUMN_NAME"])
            if columns.splitlines() != ['content\tlongtext', 'instructor_content\tlongtext']:
                raise RuntimeError('Course Lesson Long Text schema check failed')
            run(['docker', 'exec', name, 'mariadb-check', '--defaults-extra-file=/run/claw-client.cnf', '--check', 'claw_restore_verify'])
            print(f'ISOLATED_RESTORE_PASS tables={tables} attachment_files={restored_files}; both lesson fields remain Long Text; active preprod unchanged')
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)


if __name__ == '__main__':
    verify(Path(sys.argv[1]).resolve())
