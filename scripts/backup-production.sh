#!/usr/bin/env bash
# Create a complete production backup. Finalize local retention ONLY after
# the caller has uploaded and verified this archive in S3. No control host.
set -Eeuo pipefail
mode=create
label=''
backup_file=''
if [[ "${1:-}" == finalize ]]; then
    [[ $# == 2 ]] || { echo 'Usage: backup-production.sh finalize <backup-file.tgz>' >&2; exit 2; }
    mode=finalize; backup_file="$2"
    [[ "$backup_file" =~ ^lms-backup(-[a-z]+)?-[0-9]{8}T[0-9]{6}Z\.tgz$ ]] || exit 2
else
    [[ $# -le 1 ]] || { echo 'Usage: backup-production.sh [fresh|daily]' >&2; exit 2; }
    label="${1:-}"
    case "$label" in ''|fresh|daily) ;; *) exit 2 ;; esac
fi
readonly compose_dir="${TUTOR_COMPOSE_DIR:-/srv/tutor/frappe_docker}"
readonly site="${TUTOR_SITE:-learn.vladastay.ru}"
readonly backups_dir="/home/frappe/frappe-bench/sites/$site/private/backups"
readonly lock_file="${TUTOR_BACKUP_LOCK_FILE:-/run/lock/lms-backup-production.lock}"
exec 9>"$lock_file"
flock -w 600 9 || { echo 'FAILED: production backup lock unavailable' >&2; exit 1; }
cd "$compose_dir"
compose() {
    docker compose -f compose.yaml -f overrides/compose.mariadb.yaml \
        -f overrides/compose.redis.yaml -f overrides/compose.https.yaml "$@"
}
staging_dir=''
archive_path=''
metadata=''
cleanup_on_exit() {
    local code=$?
    [[ -z "$staging_dir" ]] || rm -rf -- "$staging_dir"
    if [[ "$code" -ne 0 && "$mode" == create ]]; then
        [[ -z "$archive_path" ]] || rm -f -- "$archive_path"
        [[ -z "$metadata" ]] || rm -f -- "$metadata"
    fi
}
trap cleanup_on_exit EXIT
if [[ "$mode" == create ]]; then
    # Stale staging archives are reproducible; production backup sets are not
    # pruned until finalize is called after verified S3 upload.
    find /tmp -maxdepth 1 -name 'lms-backup-*.tgz' -mmin +60 -delete 2>/dev/null || true
    compose exec -T backend bench --site "$site" backup --with-files </dev/null 2>&1 | tail -2
    cid=$(compose ps -q backend)
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    prefix=lms-backup; [[ -z "$label" ]] || prefix="$prefix-$label"
    backup_file="$prefix-$ts.tgz"
    archive_path="/tmp/$backup_file"
    metadata="$archive_path.retention.json"
    staging_dir=$(mktemp -d /tmp/lms-backup-staging.XXXXXX)
    backup_names=()
    while IFS= read -r f; do
        [[ -z "$f" ]] || backup_names+=("$f")
    done < <(docker exec "$cid" ls -t "$backups_dir" | head -4)
    [[ ${#backup_names[@]} == 4 ]] || { echo 'FAILED: expected four backup members' >&2; exit 1; }
    for f in "${backup_names[@]}"; do
        [[ "$f" != */* ]] || exit 1
        docker cp "$cid:$backups_dir/$f" "$staging_dir/" 2>/dev/null
        [[ -s "$staging_dir/$f" ]] || { echo 'FAILED: empty backup member' >&2; exit 1; }
    done
    (cd "$staging_dir" && sha256sum ./* > SHA256SUMS && sha256sum -c SHA256SUMS)
    tar czf "$archive_path" -C "$staging_dir" .
    backup_sha256=$(sha256sum "$archive_path" | awk '{print $1}')
    backup_size=$(stat -c%s "$archive_path")
    python3 - "$metadata" "$backup_sha256" "$site" "${backup_names[@]}" <<'MANIFEST'
import json,os,re,sys
names=sys.argv[4:]
pattern=re.compile(r'^(\d{8}_\d{6})-'+re.escape(sys.argv[3].replace('.','_'))+r'-(database\.sql\.gz|files\.tar(?:\.gz)?|private-files\.tar(?:\.gz)?|site_config_backup\.json)$')
matches=[pattern.fullmatch(n) for n in names]
if len(names)!=4 or len(set(names))!=4 or not all(matches):raise SystemExit('FAILED: invalid backup members')
if len({m[1] for m in matches})!=1:raise SystemExit('FAILED: mixed backup timestamps')
kinds={'config' if m[2]=='site_config_backup.json' else 'database' if m[2]=='database.sql.gz' else 'private' if m[2].startswith('private-') else 'public' for m in matches}
if kinds!={'database','public','private','config'}:raise SystemExit('FAILED: incomplete backup set')
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as f:json.dump({'sha256':sys.argv[2],'members':names},f)
MANIFEST
    printf 'BACKUP_FILE=%s\nBACKUP_SHA256=%s\nBACKUP_SIZE=%s\n' "$backup_file" "$backup_sha256" "$backup_size"
else
    archive_path="/tmp/$backup_file"
    metadata="$archive_path.retention.json"
    cid=$(compose ps -q backend)
    # Validate the retained archive before allowing any local deletion.
    python3 - "$archive_path" "$metadata" <<'CHECK'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);m=json.loads(pathlib.Path(sys.argv[2]).read_text())
h=hashlib.sha256()
with p.open('rb') as f:
 for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
if h.hexdigest()!=m['sha256']:raise SystemExit('FAILED: archive changed; local sets retained')
if len(m['members'])!=4:raise SystemExit('FAILED: invalid backup manifest')
CHECK
    backup_names=()
    while IFS= read -r f; do backup_names+=("$f"); done < <(python3 -c 'import json,sys;print("\n".join(json.load(open(sys.argv[1]))["members"]))' "$metadata")
    docker exec -i "$cid" python3 - "$backups_dir" "$site" "${backup_names[@]}" <<'PY_LOCAL_RETENTION'
import pathlib
import re
import sys

folder = pathlib.Path(sys.argv[1])
site = sys.argv[2].replace('.', '_')
names = sys.argv[3:]
pattern = re.compile(
    r'^(\d{8}_\d{6})-' + re.escape(site)
    + r'-(database\.sql\.gz|files\.tar(?:\.gz)?|private-files\.tar(?:\.gz)?|site_config_backup\.json)$'
)
required = {'database', 'public', 'private', 'config'}

def kind(suffix):
    if suffix == 'database.sql.gz':
        return 'database'
    if suffix == 'site_config_backup.json':
        return 'config'
    return 'private' if suffix.startswith('private-') else 'public'

matches = [pattern.fullmatch(name) for name in names]
if len(names) != 4 or len(set(names)) != 4 or not all(matches):
    raise SystemExit('FAILED: local retention requires four canonical backup members')
stamps = {match[1] for match in matches}
if len(stamps) != 1 or {kind(match[2]) for match in matches} != required:
    raise SystemExit('FAILED: mixed or incomplete local backup set; nothing deleted')
for name in names:
    path = folder / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise SystemExit('FAILED: local backup member missing/empty; nothing deleted')
keep_stamp = stamps.pop()
removed = 0
for path in folder.iterdir():
    match = pattern.fullmatch(path.name)
    if match and match[1] < keep_stamp and path.is_file() and not path.is_symlink():
        path.unlink()
        removed += 1
print(f'Local retention: kept {keep_stamp} (4 files), removed {removed} older files')
PY_LOCAL_RETENTION
    rm -f -- "$metadata"
fi
