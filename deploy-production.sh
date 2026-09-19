#!/usr/bin/env bash

set -Eeuo pipefail

readonly SSH_TARGET="${TUTOR_SSH_TARGET:-tutor-vps}"
readonly COMPOSE_DIR="${TUTOR_COMPOSE_DIR:-/srv/tutor/frappe_docker}"
readonly SITE="${TUTOR_SITE:-learn.vladastay.ru}"
readonly IMAGE="${TUTOR_IMAGE:-ghcr.io/sergey-olshansky/tutor-learning}"
readonly PUBLIC_URL="${TUTOR_PUBLIC_URL:-https://learn.vladastay.ru/lms}"
readonly GHCR_USER="${TUTOR_GHCR_USER:-sergey-olshansky}"

usage() {
	printf 'Usage: %s <image-tag>\n' "${0##*/}" >&2
	printf 'Example: %s lms-e0f33124\n' "${0##*/}" >&2
}

if [[ $# -ne 1 ]]; then
	usage
	exit 2
fi

readonly IMAGE_TAG="$1"
if [[ ! "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
	printf 'Invalid image tag: %s\n' "$IMAGE_TAG" >&2
	exit 2
fi

for command_name in gh ssh curl; do
	if ! command -v "$command_name" >/dev/null 2>&1; then
		printf 'Required command is missing: %s\n' "$command_name" >&2
		exit 1
	fi
done

gh auth status >/dev/null
ssh -o BatchMode=yes "$SSH_TARGET" true

readonly IMAGE_REF="${IMAGE}:${IMAGE_TAG}"
printf 'Pulling %s on %s...\n' "$IMAGE_REF" "$SSH_TARGET"

# Send the GitHub token only to docker login over SSH stdin. Docker stores it
# in a temporary config directory which is removed immediately after the pull.
set +x
github_token=$(gh auth token)
printf '%s' "$github_token" | ssh -o BatchMode=yes "$SSH_TARGET" \
	"set -Eeuo pipefail
	temp_docker_config=\$(mktemp -d /tmp/tutor-ghcr.XXXXXX)
	trap 'rm -rf \"\$temp_docker_config\"' EXIT
	docker --config \"\$temp_docker_config\" login ghcr.io --username '$GHCR_USER' --password-stdin >/dev/null
	docker --config \"\$temp_docker_config\" pull '$IMAGE_REF'"
unset github_token

printf 'Backing up the site and switching production to %s...\n' "$IMAGE_TAG"
ssh -o BatchMode=yes "$SSH_TARGET" bash -s -- \
	"$COMPOSE_DIR" "$SITE" "$IMAGE" "$IMAGE_TAG" <<'REMOTE_SCRIPT'
set -Eeuo pipefail

readonly compose_dir="$1"
readonly site="$2"
readonly image="$3"
readonly new_tag="$4"
readonly env_file="${compose_dir}/.env"
readonly timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
readonly env_backup="${compose_dir}/.env.pre-deploy-${timestamp}"
readonly services=(backend frontend websocket scheduler queue-short queue-long)
readonly compose_files=(
	-f compose.yaml
	-f overrides/compose.mariadb.yaml
	-f overrides/compose.redis.yaml
	-f overrides/compose.https.yaml
)

compose() {
	docker compose "${compose_files[@]}" "$@"
}

cd "$compose_dir"
test -f "$env_file"
docker image inspect "${image}:${new_tag}" >/dev/null
docker run --rm --entrypoint bash "${image}:${new_tag}" -lc \
	'cd /home/frappe/frappe-bench && env/bin/python -c "import PIL, pdfplumber; from lms.lms.chemedge_pdf_import import import_pdf_trainer"'

current_tag=$(awk -F= '$1 == "CUSTOM_TAG" {print $2; exit}' "$env_file")
if [[ -z "$current_tag" ]]; then
	printf 'CUSTOM_TAG is missing from %s\n' "$env_file" >&2
	exit 1
fi

printf 'Current production tag: %s\n' "$current_tag"
printf 'Creating Frappe backup before deployment...\n'
compose exec -T backend bench --site "$site" backup --with-files </dev/null

cp -a "$env_file" "$env_backup"
temp_env=$(mktemp "${compose_dir}/.env.deploy.XXXXXX")
trap 'rm -f "$temp_env"' EXIT
awk -v tag="$new_tag" '
	BEGIN { updated = 0 }
	$0 ~ /^CUSTOM_TAG=/ { print "CUSTOM_TAG=" tag; updated = 1; next }
	{ print }
	END { if (!updated) print "CUSTOM_TAG=" tag }
' "$env_file" > "$temp_env"
chmod --reference="$env_file" "$temp_env"
chown --reference="$env_file" "$temp_env"
mv "$temp_env" "$env_file"

if ! compose up -d --pull never --no-deps --force-recreate "${services[@]}"; then
	printf 'Container recreation failed; restoring tag %s...\n' "$current_tag" >&2
	cp -a "$env_backup" "$env_file"
	compose up -d --pull never --no-deps --force-recreate "${services[@]}" || true
	exit 1
fi

backend_ready=0
for attempt in {1..30}; do
	if compose exec -T backend bench --site "$site" list-apps </dev/null >/dev/null 2>&1; then
		backend_ready=1
		break
	fi
	sleep 2
done
if [[ "$backend_ready" -ne 1 ]]; then
	printf 'New backend did not become ready; restoring tag %s...\n' "$current_tag" >&2
	cp -a "$env_backup" "$env_file"
	compose up -d --pull never --no-deps --force-recreate "${services[@]}" || true
	exit 1
fi

# Once migrations begin, automatic downgrade is unsafe. Keep the backup and
# the new containers in place so recovery can be planned without data loss.
if ! compose exec -T backend bench --site "$site" migrate </dev/null; then
	printf 'Migration failed. Production remains on %s; previous env: %s\n' \
		"$new_tag" "$env_backup" >&2
	exit 1
fi

compose exec -T backend bench --site "$site" clear-cache </dev/null
compose ps "${services[@]}"
printf 'Previous env backup: %s\n' "$env_backup"
REMOTE_SCRIPT

deployed_tag=$(ssh -o BatchMode=yes "$SSH_TARGET" \
	"awk -F= '\$1 == \"CUSTOM_TAG\" {print \$2; exit}' '$COMPOSE_DIR/.env'")
if [[ "$deployed_tag" != "$IMAGE_TAG" ]]; then
	printf 'Tag verification failed: expected %s, production has %s\n' \
		"$IMAGE_TAG" "$deployed_tag" >&2
	exit 1
fi

printf 'Waiting for %s...\n' "$PUBLIC_URL"
for attempt in {1..20}; do
	if curl --fail --silent --show-error --max-time 15 "$PUBLIC_URL" >/dev/null; then
		printf 'Production is healthy on %s with tag %s.\n' "$PUBLIC_URL" "$IMAGE_TAG"
		exit 0
	fi
	sleep 3
done

printf 'Containers were updated, but the public health check failed: %s\n' "$PUBLIC_URL" >&2
exit 1
