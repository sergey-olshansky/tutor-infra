#!/usr/bin/env bash
# Explicit owner invocation delegates to the same S3-gated release workflow.
set -Eeuo pipefail
[[ $# == 1 ]] || { echo "Usage: ${0##*/} <immutable-image-tag>" >&2; exit 2; }
tag="$1"
[[ "$tag" =~ ^lms-[0-9a-f]{8}-infra-[0-9a-f]{8}-run-[0-9]+-[0-9]+$ ]] || { echo 'Expected the immutable tag accepted on preprod.' >&2; exit 2; }
command -v gh >/dev/null
repo=sergey-olshansky/tutor-infra
request_id="manual-$(python3 -c 'import uuid;print(uuid.uuid4().hex)')"
gh workflow run deploy-production.yml --repo "$repo" --ref main \
    -f "image_tag=$tag" -f "request_id=$request_id" -f dry_run=false
run_id=''
for attempt in $(seq 1 24); do
    run_id=$(gh run list --repo "$repo" --workflow deploy-production.yml --branch main \
        --event workflow_dispatch --limit 100 --json databaseId,displayTitle \
        --jq ".[] | select(.displayTitle | startswith(\"production-$request_id-\")) | .databaseId" | head -n 1)
    [[ -z "$run_id" ]] || break
    sleep 5
done
[[ -n "$run_id" ]] || { echo "Dispatch accepted but run not yet visible; inspect request $request_id before retrying." >&2; exit 1; }
printf 'Release: https://github.com/%s/actions/runs/%s\n' "$repo" "$run_id"
gh run watch "$run_id" --repo "$repo" --exit-status
