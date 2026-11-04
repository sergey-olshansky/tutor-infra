#!/usr/bin/env bash

# S3-помощник для бекапов LMS production; выполняется НА раннере GitHub Actions
# (предустановлены aws cli и python3). Работает с любым S3-совместимым провайдером
# через S3_ENDPOINT_URL.
#
# Каноническое имя бекапа (единая строгая проверка в upload/latest/retention/
# download, посторонние объекты не выбираются и не удаляются):
#   lms-backup[-label]-YYYYMMDDTHHMMSSZ.tgz   (label: fresh|daily|…)
#
# Команды:
#   upload <lms-backup-*.tgz>          — загрузка + .sha256 сайдкар, проверка
#                                        размера через head-object, затем ротация;
#                                        печатает маркер S3_KEY=<prefix>/<имя>
#   download <key|latest> <dest-path>  — скачивание (latest = новейший по дате
#                                        в имени) с проверкой sha256 по сайдкару;
#                                        <key> принимает как полный ключ из
#                                        маркера S3_KEY (<prefix>/<имя>), так и
#                                        само имя
#   retention                          — удалить канонические бекапы старее
#                                        S3_RETAIN_DAYS
#
# Переменные окружения:
#   S3_BUCKET          — обязателен (пусто -> ошибка конфигурации)
#   S3_ENDPOINT_URL    — пусто = AWS; иначе https://<endpoint> провайдера
#   S3_REGION          — по умолчанию us-east-1
#   S3_PREFIX          — по умолчанию lms-production
#   S3_RETAIN_DAYS     — по умолчанию 30; 0/пусто = ротация выключена
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY — креды
set -Eeuo pipefail

usage() {
	printf 'Usage: %s upload <file.tgz> | download <key|latest> <dest> | retention\n' "$0" >&2
	exit 2
}

[[ $# -ge 1 ]] || usage
command -v aws >/dev/null || { printf 'aws cli is missing\n' >&2; exit 1; }
command -v python3 >/dev/null || { printf 'python3 is missing\n' >&2; exit 1; }

: "${S3_BUCKET:?S3_BUCKET is required}"
readonly prefix="${S3_PREFIX:-lms-production}"
readonly retain_days="${S3_RETAIN_DAYS:-30}"
export AWS_DEFAULT_REGION="${S3_REGION:-us-east-1}"

s3() {
	if [[ -n "${S3_ENDPOINT_URL:-}" ]]; then
		aws s3 --endpoint-url "$S3_ENDPOINT_URL" "$@"
	else
		aws s3 "$@"
	fi
}
s3api() {
	if [[ -n "${S3_ENDPOINT_URL:-}" ]]; then
		aws s3api --endpoint-url "$S3_ENDPOINT_URL" "$@"
	else
		aws s3api "$@"
	fi
}

is_canonical_name() {
	[[ "$1" =~ ^lms-backup(-[a-z]+)?-[0-9]{8}T[0-9]{6}Z\.tgz$ ]]
}

file_size() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1"; }
# Дата YYYYMMDD из канонического имени (для ротации по дням).
key_date() { printf '%s' "$1" | sed -nE 's/.*-([0-9]{8})T[0-9]{6}Z\.tgz$/\1/p'; }
# Полное время YYYYMMDDTHHMMSSZ из канонического имени (для выбора новейшего:
# одного дня недостаточно — внутри дня лейблы ломали бы порядок).
key_stamp() { printf '%s' "$1" | sed -nE 's/.*-([0-9]{8}T[0-9]{6}Z)\.tgz$/\1/p'; }

# Листинг префикса с явной проверкой отказа: ошибка s3 ls обязана валить
# вызывавшую команду, а не превращаться в молчаливое «удалено 0».
fetch_keys() {
	local listing
	if ! listing=$(s3 ls "s3://$S3_BUCKET/$prefix/"); then
		printf 'FAILED: cannot list s3://%s/%s/\n' "$S3_BUCKET" "$prefix" >&2
		return 1
	fi
	# Поле 4+ в выводе `aws s3 ls` — ключ (в наших ключах пробелов нет).
	printf '%s\n' "$listing" | awk 'NF >= 4 {print substr($0, index($0, $4))}'
}

retention() {
	if [[ ! "$retain_days" =~ ^[0-9]+$ || "$retain_days" -eq 0 ]]; then
		printf 'S3 retention disabled (S3_RETAIN_DAYS=%s)\n' "$retain_days"
		return 0
	fi
	local cutoff keys key date removed=0
	cutoff=$(python3 -c 'import sys,datetime; print((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=int(sys.argv[1]))).strftime("%Y%m%d"))' "$retain_days")
	keys=$(fetch_keys)
	while IFS= read -r key; do
		is_canonical_name "$key" || continue
		date=$(key_date "$key")
		if [[ "$date" < "$cutoff" ]]; then
			s3 rm "s3://$S3_BUCKET/$prefix/$key" >/dev/null
			s3 rm "s3://$S3_BUCKET/$prefix/$key.sha256" >/dev/null || true
			printf 'retention: removed %s\n' "$key"
			removed=$((removed + 1))
		fi
	done <<< "$keys"
	printf 'retention: kept backups not older than %s days (cutoff %s), removed %s\n' \
		"$retain_days" "$cutoff" "$removed"
}

upload() {
	[[ $# -eq 1 ]] || usage
	local file="$1" base sha256 size remote_size
	base=$(basename "$file")
	[[ -f "$file" ]] || { printf 'No such file: %s\n' "$file" >&2; exit 1; }
	# Ротация и latest опираются на дату в имени — принимаем только канонические имена.
	is_canonical_name "$base" || {
		printf 'Refusing to upload: name does not match lms-backup[-label]-YYYYMMDDTHHMMSSZ.tgz: %s\n' "$base" >&2
		exit 1
	}

	sha256=$(sha256sum "$file" | awk '{print $1}')
	size=$(file_size "$file")
	printf '%s  %s\n' "$sha256" "$base" > "$file.sha256"

	printf 'Uploading %s (%s bytes) to s3://%s/%s/...\n' "$base" "$size" "$S3_BUCKET" "$prefix"
	s3 cp "$file" "s3://$S3_BUCKET/$prefix/$base"
	s3 cp "$file.sha256" "s3://$S3_BUCKET/$prefix/$base.sha256"

	remote_size=$(s3api head-object --bucket "$S3_BUCKET" --key "$prefix/$base" \
		--query ContentLength --output text)
	if [[ "$remote_size" != "$size" ]]; then
		printf 'FAILED: remote size %s != local %s\n' "$remote_size" "$size" >&2
		exit 1
	fi
	printf 'Verified size in S3: %s bytes\n' "$remote_size"
	retention
	printf 'S3_KEY=%s/%s\n' "$prefix" "$base"
}

download() {
	[[ $# -eq 2 ]] || usage
	local key="$1" dest="$2" sidecar_hash got_hash tmp
	# Принимаем полный ключ из маркера S3_KEY (prefix/name) или относительное имя.
	case "$key" in
		"$prefix/"*) key="${key#"$prefix"/}" ;;
	esac
	if [[ "$key" == "latest" ]]; then
		local keys
		keys=$(fetch_keys)
		# Сортируем по дате-времени из имени: лейблы (daily/fresh) не должны
		# влиять на порядок; посторонние имена отфильтрованы канонической
		# проверкой. if без else: тело цикла всегда завершается 0 (set -e).
		key=$(printf '%s\n' "$keys" | while IFS= read -r k; do
			if is_canonical_name "$k"; then
				printf '%s %s\n' "$(key_stamp "$k")" "$k"
			fi
		done | sort -r | awk 'NR==1{print $2}')
		[[ -n "$key" ]] || { printf 'No canonical lms-backup-*.tgz found in s3://%s/%s/\n' "$S3_BUCKET" "$prefix" >&2; exit 1; }
		printf 'Latest backup: %s\n' "$key"
	fi
	is_canonical_name "$key" || { printf 'Invalid key: %s\n' "$key" >&2; exit 1; }

	s3 cp "s3://$S3_BUCKET/$prefix/$key" "$dest"
	tmp=$(mktemp)
	s3 cp "s3://$S3_BUCKET/$prefix/$key.sha256" "$tmp"
	sidecar_hash=$(awk 'NR==1{print $1}' "$tmp")
	rm -f "$tmp"
	got_hash=$(sha256sum "$dest" | awk '{print $1}')
	if [[ "$got_hash" != "$sidecar_hash" ]]; then
		printf 'FAILED: sha256 mismatch for %s\n' "$key" >&2
		exit 1
	fi
	printf 'Verified sha256 of %s\n' "$key"
}

case "$1" in
	upload) shift; upload "$@" ;;
	download) shift; download "$@" ;;
	retention) shift; [[ $# -eq 0 ]] || usage; retention ;;
	*) usage ;;
esac
