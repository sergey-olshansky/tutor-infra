"""Require successful original build evidence for the accepted task image."""
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    tag, sha, digest = (os.environ[k] for k in ('SOURCE_TAG', 'SOURCE_SHA', 'SOURCE_DIGEST'))
    m = re.fullmatch(r'task-([1-9][0-9]*)-lms-([a-f0-9]{12})-infra-([a-f0-9]{12})-run-([1-9][0-9]*)-([1-9][0-9]*)', tag)
    if not m or not re.fullmatch(r'[a-f0-9]{40}', sha) or sha[:12] != m[2] or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('Invalid accepted candidate identity')
    repo = 'sergey-olshansky/tutor-infra'
    run = json.loads(subprocess.check_output(['gh', 'api', f'repos/{repo}/actions/runs/{m[4]}']))
    if not (run['conclusion'] == 'success' and run['run_attempt'] == int(m[5]) and run['head_sha'].startswith(m[3])
            and run['head_branch'] == 'main' and run['event'] == 'workflow_dispatch'
            and run['path'].split('@')[0] == '.github/workflows/build.yml' and run['display_title'].endswith('-' + sha)):
        raise ValueError('Candidate build provenance is not a successful pinned build')
    directory = Path('accepted-verification')
    subprocess.run(['gh', 'run', 'download', m[4], '--repo', repo, '--name', 'image-verification-' + sha, '--dir', str(directory)], check=True)
    report = json.loads((directory / 'report.json').read_text())
    if not (report.get('status') == 'VERIFIED' and report.get('revision') == sha
            and report.get('image') == 'ghcr.io/sergey-olshansky/tutor-learning@' + digest):
        raise ValueError('Accepted image verification does not match source SHA/digest')
    subprocess.run(['python3', 'scripts/registry-image.py', 'resolve', '--source', tag, '--output', 'accepted-image.json'], check=True)
    if json.loads(Path('accepted-image.json').read_text()).get('digest') != digest:
        raise ValueError('Accepted task tag no longer resolves to its verified digest')
    print('Accepted candidate provenance verified; promotion must preserve this digest')


if __name__ == '__main__':
    main()
