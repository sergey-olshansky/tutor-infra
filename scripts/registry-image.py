"""Resolve/copy immutable GHCR manifests without rebuilding image layers."""
import argparse
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

REPO = 'sergey-olshansky/tutor-learning'
ACCEPT = ', '.join(['application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json', 'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['resolve', 'copy'])
    p.add_argument('--source', required=True)
    p.add_argument('--target')
    p.add_argument('--output', required=True)
    a = p.parse_args()
    if not re.fullmatch(r'(?:sha256:[a-f0-9]{64}|[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})', a.source):
        p.error('Invalid source reference')
    if a.action == 'copy' and (not a.target or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', a.target)):
        p.error('Invalid target tag')
    auth = base64.b64encode(f"{os.environ['GITHUB_ACTOR']}:{os.environ['GHCR_TOKEN']}".encode()).decode()
    scope = 'pull,push' if a.action == 'copy' else 'pull'
    url = 'https://ghcr.io/token?' + urllib.parse.urlencode({'service': 'ghcr.io', 'scope': f'repository:{REPO}:{scope}'})
    req = urllib.request.Request(url, headers={'Authorization': 'Basic ' + auth})
    with urllib.request.urlopen(req, timeout=60) as r:
        token = json.load(r)['token']

    def request(ref, data=None, media=None):
        headers = {'Authorization': 'Bearer ' + token, 'Accept': ACCEPT}
        if media:
            headers['Content-Type'] = media
        req = urllib.request.Request(f'https://ghcr.io/v2/{REPO}/manifests/{ref}', data=data, headers=headers, method='PUT' if data is not None else 'GET')
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(), r.headers.get('Content-Type', '').split(';')[0]
        except urllib.error.HTTPError as e:
            if e.code == 404 and data is None:
                return None, None
            # Do not print authorization headers or response bodies.
            raise RuntimeError(f'GHCR manifest operation failed: HTTP {e.code}') from None

    raw, media = request(a.source)
    result = {'found': raw is not None, 'source': a.source}
    if raw is not None:
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if a.source.startswith('sha256:') and digest != a.source:
            raise RuntimeError('Source manifest does not match its digest')
        result['digest'] = digest
        if a.action == 'copy':
            old, _ = request(a.target)
            if old is not None and old != raw:
                raise RuntimeError('Target tag already names different bytes; refusing overwrite')
            if old is None:
                request(a.target, raw, media)
            actual, _ = request(a.target)
            if actual != raw:
                raise RuntimeError('Copied manifest bytes differ')
            result.update(target=a.target, reused=True)
    elif a.action == 'copy':
        raise RuntimeError('Source manifest is missing')
    with open(a.output, 'w') as f:
        json.dump(result, f, indent=2)
        f.write('\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            f.write(f"found={str(result['found']).lower()}\ndigest={result.get('digest', '')}\n")
    print(json.dumps(result))


if __name__ == '__main__':
    main()
