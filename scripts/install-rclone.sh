#!/usr/bin/env bash

# Устанавливает зафиксированную версию rclone v1.75.2 (linux-amd64) — на
# GitHub-раннер или на сервер (production/preprod; уже установлен на обоих).
# S3-бекапы используют rclone: AWS CLI v2 (стек TLS s2n) не соединяется с
# Selectel — см. .github/workflows/s3-net-diagnostic.yml и BACKUP.md.
# На серверах Ubuntu без unzip: python3 -m zipfile -e r.zip /tmp/rx
set -Eeuo pipefail

readonly version='1.75.2'
readonly sha256='349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702'
readonly url="https://downloads.rclone.org/v${version}/rclone-v${version}-linux-amd64.zip"

zip=$(mktemp)
trap 'rm -f "$zip"' EXIT
curl -fsSL -o "$zip" "$url"
printf '%s  %s\n' "$sha256" "$zip" | sha256sum -c -
unzip -q -j -o "$zip" -d /usr/local/bin
rclone version | head -1
