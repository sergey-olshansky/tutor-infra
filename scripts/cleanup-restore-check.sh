#!/usr/bin/env bash
set -euo pipefail
ROOT=${1:?scratch root required}
NAME=${2:?container name required}
[[ "$ROOT" =~ ^/var/tmp/claw-s3-verify-[0-9]+-[0-9]+$ ]] || exit 2
[[ "$NAME" =~ ^claw-restore-check-[0-9]+-[0-9]+$ ]] || exit 2
docker info >/dev/null 2>&1
if ! docker rm -f "$NAME" >/dev/null 2>&1; then
    # A missing container is normal; failure to stop an existing one is not.
    if docker inspect "$NAME" >/dev/null 2>&1; then exit 1; fi
fi
if mountpoint -q "$ROOT/restore-scratch"; then
    umount "$ROOT/restore-scratch"
fi
rm -f -- "$ROOT/restore-scratch.ext4"
if [[ -d "$ROOT/restore-scratch" ]]; then rmdir -- "$ROOT/restore-scratch"; fi
