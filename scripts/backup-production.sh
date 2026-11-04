#!/usr/bin/env bash

# Общий бекап production LMS: bench backup -> lms-backup-*.tgz (+SHA256SUMS)
# -> отправка на control (старый путь, остаётся). Выполняется НА production
# через `ssh root@PROD bash -s -- <control-host> [label] < scripts/backup-production.sh`.
# Готовый tgz остаётся в /tmp — раннер забирает его для загрузки в S3 и затем
# удаляет. Маркеры для раннера (stdout): BACKUP_FILE=, BACKUP_SHA256=, BACKUP_SIZE=.
# Параллельные вызовы сериализуются локом /run/lock/lms-backup-production.lock.
#
# bench backup с --with-files всегда делает свежий дамп (force=True) и сам
# удаляет файлы старше ~23 ч (frappe delete_temp_backups), поэтому набор бекапов
# в контейнере не растёт.
set -Eeuo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
	printf 'Usage: %s <control-host> [label]\n' "$0" >&2
	exit 2
fi
control_host="$1"
label="${2:-}"
case "$label" in
	"") name_prefix='lms-backup' ;;
	fresh|daily) name_prefix="lms-backup-$label" ;;
	*) printf 'Invalid label: %s (allowed: fresh, daily)\n' "$label" >&2; exit 2 ;;
esac

readonly compose_dir="${TUTOR_COMPOSE_DIR:-/srv/tutor/frappe_docker}"
readonly site="${TUTOR_SITE:-learn.vladastay.ru}"
readonly control_user='backuprecv'
readonly control_key='/root/.ssh/backup-to-control'
readonly backups_dir="/home/frappe/frappe-bench/sites/$site/private/backups"
readonly lock_file="${TUTOR_BACKUP_LOCK_FILE:-/run/lock/lms-backup-production.lock}"

# Все три workflow (release, clone, daily) имеют разные concurrency groups в
# GitHub Actions, поэтому сериализуем бекапы на самом сервере. Лок держится до
# выхода скрипта: bench backup и выбор четырёх свежих файлов работают с общим
# каталогом сайта, гонки недопустимы.
exec 9>"$lock_file"
if ! flock -w 600 9; then
	printf 'FAILED: another lms backup holds %s\n' "$lock_file" >&2
	exit 1
fi

# Staging-каталог убираем при любом выходе; tgz — только при сбое (при успехе
# его забирает раннер). Каталог уникален на запуск — параллельные запуски (если
# лок недоступен, например при ручном вызове) не конфликтуют.
staging_dir=''
archive_path=''
cleanup_on_exit() {
	local code=$?
	[[ -z "$staging_dir" ]] || rm -rf -- "$staging_dir"
	if [[ "$code" -ne 0 && -n "$archive_path" ]]; then
		rm -f -- "$archive_path"
	fi
}
trap cleanup_on_exit EXIT

cd "$compose_dir"
compose() {
	docker compose -f compose.yaml -f overrides/compose.mariadb.yaml \
		-f overrides/compose.redis.yaml -f overrides/compose.https.yaml "$@"
}

# Убираем tgz от прерванных запусков (старше часа), чтобы /tmp не рос.
find /tmp -maxdepth 1 -name 'lms-backup-*.tgz' -mmin +60 -delete 2>/dev/null || true

printf '== Creating bench backup (with files)...\n'
compose exec -T backend bench --site "$site" backup --with-files </dev/null 2>&1 | tail -2

cid=$(compose ps -q backend)
ts=$(date -u +%Y%m%dT%H%M%SZ)
staging_dir=$(mktemp -d /tmp/lms-backup-staging.XXXXXX)
# Свежий сет — 4 новейших файла: БД, публичные/приватные файлы, site config.
for f in $(docker exec "$cid" ls -t "$backups_dir" | head -4); do
	docker cp "$cid:$backups_dir/$f" "$staging_dir/" 2>/dev/null
done
file_count=$(ls -1 "$staging_dir" | wc -l)
if [[ "$file_count" -ne 4 ]]; then
	printf 'FAILED: expected 4 backup files, got %s\n' "$file_count" >&2
	exit 1
fi
(cd "$staging_dir" && sha256sum ./* > SHA256SUMS)

backup_file="${name_prefix}-${ts}.tgz"
archive_path="/tmp/$backup_file"
tar czf "$archive_path" -C "$staging_dir" .
backup_sha256=$(sha256sum "$archive_path" | awk '{print $1}')
backup_size=$(stat -c%s "$archive_path")

printf '== Sending %s to control (%s)...\n' "$backup_file" "$control_host"
ssh -i "$control_key" -o BatchMode=yes \
	"$control_user@$control_host" "put $backup_file" < "$archive_path"

rm -rf "$staging_dir"
staging_dir=''
printf 'BACKUP_FILE=%s\n' "$backup_file"
printf 'BACKUP_SHA256=%s\n' "$backup_sha256"
printf 'BACKUP_SIZE=%s\n' "$backup_size"
