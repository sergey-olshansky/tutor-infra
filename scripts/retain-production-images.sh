#!/usr/bin/env bash

# Run on the Docker host before pull, then after migration and HTTP health pass.
set -Eeuo pipefail
umask 077

if [[ $# != 4 || ! "$1" =~ ^(prepare|complete)$ ]]; then
	printf 'Usage: %s prepare|complete <compose-dir> <image-repository> <target-tag>\n' "$0" >&2
	exit 2
fi
phase="$1"; compose_dir="$2"; image="$3"; target_tag="$4"
valid_tag() { [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; }
valid_tag "$target_tag" || { echo 'Invalid target tag' >&2; exit 2; }
cd "$compose_dir"
current_tag=$(awk -F= '$1=="CUSTOM_TAG" {print $2; exit}' .env)
valid_tag "$current_tag" || { echo 'Missing or invalid CUSTOM_TAG' >&2; exit 1; }
previous_file=.previous-production-tag
plan=".release-images-${target_tag}"
temp_file=''
trap 'rm -f -- "${temp_file:-}"' EXIT

image_exists() { docker image inspect "$image:$1" >/dev/null 2>&1; }
rollback_candidate() {
	valid_tag "$1" && [[ "$1" != "$target_tag" ]] && image_exists "$1"
}

prune_images() {
	local tag id repository ref keep_ids=' ' listing container_list used_ids=' ' container_image_ids
	local -a container_ids=()
	for tag in "$@"; do
		[[ -n "$tag" ]] || continue
		if id=$(docker image inspect --format '{{.Id}}' "$image:$tag" 2>/dev/null); then
			keep_ids+="$id "
		fi
	done
	container_list=$(docker container ls -aq)
	if [[ -n "$container_list" ]]; then
		read -r -a container_ids <<< "${container_list//$'\n'/ }"
		container_image_ids=$(docker container inspect --format '{{.Image}}' "${container_ids[@]}")
		used_ids=" ${container_image_ids//$'\n'/ } "
	fi
	# A failed inventory must not be mistaken for an empty successful cleanup.
	listing=$(docker image ls --no-trunc --format '{{.Repository}} {{.Tag}} {{.ID}}' "$image")
	while read -r repository tag id; do
		[[ "$repository" == "$image" && "$tag" != '<none>' ]] || continue
		[[ "$keep_ids" != *" $id "* ]] || continue
		ref="$repository:$tag"
		if [[ "$used_ids" == *" $id "* ]]; then
			printf 'WARNING: retained %s; a container still references this image.\n' "$ref" >&2
			continue
		fi
		printf 'Removing old production image: %s\n' "$ref"
		# Never force removal: Docker protects images referenced by containers.
		if ! docker image rm "$ref"; then
			printf 'WARNING: retained %s; Docker refused removal (possibly in use).\n' "$ref" >&2
		fi
	done <<< "$listing"
}

if [[ "$phase" == prepare ]]; then
	# Current production must remain available even if the incoming pull fails.
	image_exists "$current_tag" || { echo 'Current production image is missing; cleanup refused' >&2; exit 1; }
	rollback_tag="$current_tag"
	if [[ "$current_tag" == "$target_tag" ]]; then
		# Retrying the same release must keep its actual predecessor, not just itself.
		rollback_tag=''
		if [[ -f "$plan" && "$(sed -n '1p' "$plan")" == "$image" && "$(sed -n '2p' "$plan")" == "$target_tag" ]]; then
			candidate=$(sed -n '3p' "$plan")
			if rollback_candidate "$candidate"; then rollback_tag="$candidate"; fi
		fi
		if [[ -z "$rollback_tag" && -f "$previous_file" ]]; then
			candidate=$(head -n 1 "$previous_file")
			if rollback_candidate "$candidate"; then rollback_tag="$candidate"; fi
		fi
		if [[ -z "$rollback_tag" ]]; then
			# Bootstrap retention on hosts deployed before this marker existed.
			shopt -s nullglob
			backups=(.env.pre-deploy-*)
			for ((i=${#backups[@]}-1; i>=0; i--)); do
				candidate=$(awk -F= '$1=="CUSTOM_TAG" {print $2; exit}' "${backups[i]}")
				if rollback_candidate "$candidate"; then rollback_tag="$candidate"; break; fi
			done
		fi
	fi
	prune_images "$current_tag" "$target_tag" "$rollback_tag"
	temp_file=$(mktemp "${plan}.XXXXXX")
	printf '%s\n%s\n%s\n' "$image" "$target_tag" "$rollback_tag" > "$temp_file"
	mv -- "$temp_file" "$plan"
	printf 'Retention prepared: target=%s previous=%s\n' "$target_tag" "${rollback_tag:-unavailable}"
else
	[[ "$current_tag" == "$target_tag" ]] || { echo 'Production tag changed; cleanup refused' >&2; exit 1; }
	[[ -f "$plan" && "$(sed -n '1p' "$plan")" == "$image" && "$(sed -n '2p' "$plan")" == "$target_tag" ]] || {
		echo 'Matching retention plan is missing; cleanup refused' >&2; exit 1;
	}
	rollback_tag=$(sed -n '3p' "$plan")
	image_exists "$target_tag" || { echo 'Released image is missing; cleanup refused' >&2; exit 1; }
	if [[ -n "$rollback_tag" ]]; then
		valid_tag "$rollback_tag" && image_exists "$rollback_tag" || {
			echo 'Previous image is missing; cleanup refused' >&2; exit 1;
		}
	fi
	prune_images "$target_tag" "$rollback_tag"
	temp_file=$(mktemp "${previous_file}.XXXXXX")
	printf '%s\n' "$rollback_tag" > "$temp_file"
	mv -- "$temp_file" "$previous_file"
	rm -- "$plan"
	printf 'Retention complete: current=%s previous=%s\n' "$target_tag" "${rollback_tag:-unavailable}"
fi
