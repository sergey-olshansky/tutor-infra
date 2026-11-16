#!/usr/bin/env python3
"""Send a verified production archive to the configured owner's release chat."""
import json
import hashlib
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def validate_and_extract(archive: Path, directory: Path):
    extracted = directory / 'files'
    extracted.mkdir()
    with tarfile.open(archive) as tar:
        pattern = re.compile(r'^(\d{8}_\d{6})-' + re.escape(os.environ.get('PROD_SITE', 'learn.vladastay.ru').replace('.', '_'))
                             + r'-(database\.sql\.gz|files\.tar(?:\.gz)?|private-files\.tar(?:\.gz)?|site_config_backup\.json)$')
        files = []
        for member in tar.getmembers():
            name = posixpath.normpath(member.name)
            if member.isdir() and name == '.':
                continue
            if not member.isfile() or '/' in name:
                raise RuntimeError('Invalid production backup archive member')
            files.append(name)
        if len(files) != 5 or len(set(files)) != 5 or 'SHA256SUMS' not in files:
            raise RuntimeError('Production backup must contain four files and SHA256SUMS')
        names = set(files) - {'SHA256SUMS'}
        matches = [pattern.fullmatch(name) for name in names]
        if not all(matches) or len({m[1] for m in matches}) != 1:
            raise RuntimeError('Invalid production backup file names or timestamps')
        kinds = {'config' if m[2] == 'site_config_backup.json' else 'database' if m[2] == 'database.sql.gz'
                 else 'private' if m[2].startswith('private-') else 'public' for m in matches}
        if kinds != {'database', 'public', 'private', 'config'}:
            raise RuntimeError('Incomplete production backup set')
        tar.extractall(extracted, filter='data')
    checks = {}
    for line in (extracted / 'SHA256SUMS').read_text().splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or not re.fullmatch('[0-9a-fA-F]{64}', fields[0]):
            raise RuntimeError('Invalid embedded checksum manifest')
        name = fields[1].removeprefix('*').removeprefix('./')
        if name not in names or name in checks:
            raise RuntimeError('Unexpected or duplicate checksum entry')
        checks[name] = fields[0].lower()
    if set(checks) != names:
        raise RuntimeError('Incomplete embedded checksum manifest')
    for name, expected in checks.items():
        digest = hashlib.sha256()
        with (extracted / name).open('rb') as source:
            for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise RuntimeError('Embedded backup checksums failed')
    return extracted


def prepare_parts(archive: Path, directory: Path, part_size=45 * 1024 * 1024):
    extracted = validate_and_extract(archive, directory)
    zipped = directory / 'backup.zip'
    with zipfile.ZipFile(zipped, 'w', compression=zipfile.ZIP_DEFLATED) as out:
        for path in sorted(extracted.rglob('*')):
            if path.is_file():
                out.write(path, path.relative_to(extracted))
    if zipped.stat().st_size <= part_size:
        return [zipped]
    parts = []
    with zipped.open('rb') as source:
        index = 1
        while block := source.read(part_size):
            part = directory / f'backup.zip.part-{index:03d}'
            part.write_bytes(block)
            parts.append(part)
            index += 1
    return parts


def main():
    archive = Path(sys.argv[1]).resolve()
    token = os.environ['RELEASE_TG_BOT_TOKEN']
    chat = os.environ['RELEASE_TG_CHAT_ID']
    with tempfile.TemporaryDirectory(prefix='claw-backup-send-') as tmp:
        parts = prepare_parts(archive, Path(tmp))
        for i, part in enumerate(parts, 1):
            caption = f'Production backup from S3 · part {i}/{len(parts)}'
            if len(parts) > 1 and i == len(parts):
                caption += '\ncat backup.zip.part-* > backup.zip'
            try:
                result = subprocess.run([
                    'curl', '--silent', '--show-error', '--fail-with-body', '--max-time', '180',
                    '-F', f'chat_id={chat}', '-F', f'caption={caption}',
                    '-F', f'document=@{part}', f'https://api.telegram.org/bot{token}/sendDocument',
                ], capture_output=True, text=True, timeout=190)
            except (subprocess.TimeoutExpired, OSError):
                raise RuntimeError(f'Telegram transport failed for backup part {i}/{len(parts)}') from None
            try:
                confirmed = json.loads(result.stdout).get('ok') is True
            except ValueError:
                confirmed = False
            if result.returncode or not confirmed:
                # Do not print curl arguments, token, response body or a retry
                # that might duplicate an ambiguously accepted attachment.
                raise RuntimeError(f'Telegram did not confirm backup part {i}/{len(parts)}')
            print(f'Backup part {i}/{len(parts)} delivered')


if __name__ == '__main__':
    main()
