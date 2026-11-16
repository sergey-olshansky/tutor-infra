# Бекапы production: S3 и локальный набор на production

## Рабочая схема

- Production и preprod остаются на VPS. Управляющий Raspberry Pi доступен только в домашней сети и не принимает бекапы.
- `scripts/backup-production.sh [fresh|daily]` создаёт полный набор: БД, публичные/приватные файлы, конфигурация сайта; проверяет состав и записывает архив с `SHA256SUMS`. Печатает `BACKUP_FILE`, `BACKUP_SHA256`, `BACKUP_SIZE`.
- Workflow загружает архив в S3 непосредственно с production через `scripts/s3-backup.sh upload`, проверяет размер и сохраняет SHA256-сайдкар.
- Только после успешного upload workflow вызывает `backup-production.sh finalize <имя-архива>`. Проверяется неизменность архива, после чего удаляются более старые локальные наборы этого сайта. Новейший полный набор остаётся на production.
- Ошибка создания/загрузки/проверки бекапа блокирует production-выкладку. S3 обязателен; запасного пути через control нет.
- Preprod скачивает архив из S3 и проверяет SHA256 до восстановления.
- Ежедневный запуск — `0 0 * * *` UTC, то есть 03:00 МСК; журнал — `releases/backups.log`.
- Временные S3-ключи передаются только production/preprod, в уникальный для запуска файл `/run/tutor-s3-<run>-<attempt>.env` (0600), с обязательной очисткой при сбое. На Pi S3-ключей нет.

## Получение бекапа через релизного бота

Команда владельца запускает `send-production-backup.yml`. Архив скачивается из S3 на production с проверкой SHA256, передаётся раннеру и ещё раз проверяется. Внутренние четыре файла проверяются по составу и SHA256. Бот отправляет ZIP либо части по 45 МиБ в прежний разрешённый чат. Временные ключи и архив удаляются; копия на Pi не создаётся.

Production-архивы, уже находящиеся в S3, при миграции control не копируются и не изменяются.

## Настройка S3

Секреты и переменные репозитория `sergey-olshansky/tutor-infra`:

| Имя | Тип | Пример / пусто |
| --- | --- | --- |
| `S3_ACCESS_KEY_ID` | secret | `AKIA…` / ключ провайдера |
| `S3_SECRET_ACCESS_KEY` | secret | `…` |
| `S3_BUCKET` | var | `vladastay-lms-backups` (**пусто = S3 выключен**) |
| `S3_ENDPOINT_URL` | var | пусто = AWS; иначе `https://s3.timeweb.cloud`, `https://s3.ru-1.storage.selcloud.ru`, `https://s3.yandexcloud.net`, `https://s3.us-west-004.backblazeb2.com`, … |
| `S3_REGION` | var | по умолчанию `us-east-1` |
| `S3_PREFIX` | var | по умолчанию `lms-production` |
| `S3_RETAIN_DAYS` | var | по умолчанию `30`; `0` = без ротации |

Установка с операторской машины:

```bash
gh secret set S3_ACCESS_KEY_ID      -R sergey-olshansky/tutor-infra
gh secret set S3_SECRET_ACCESS_KEY  -R sergey-olshansky/tutor-infra
gh variable set S3_BUCKET      -R sergey-olshansky/tutor-infra -b "<имя-бакета>"
gh variable set S3_ENDPOINT_URL -R sergey-olshansky/tutor-infra -b "<https://endpoint>"   # AWS: пропустить
```

Бакет должен уже существовать (скрипт его не создаёт), доступ — приватный,
полномочия ключа — только этот бакет (read/write/list). Внутри архива лежит
`SHA256SUMS` для четырёх файлов бекапа, рядом с объектом — `<файл>.sha256`.

Первичная проверка без релиза: задиспатчить «Deploy to Preprod» с `clone_prod=true`
(любой валидный тег) — шаги бекапа и S3 выполнятся до переключения образа; либо
локально: `S3_…=… scripts/s3-backup.sh download latest /tmp/check.tgz`.

## Восстановление из S3 (runbook)

```bash
# rclone (тот же клиент, что в пайплайне); creds и endpoint — как в vars/secrets
export RCLONE_CONFIG_BK_TYPE=s3 RCLONE_CONFIG_BK_PROVIDER=Other \
  RCLONE_CONFIG_BK_ENDPOINT=https://s3.ru-7.storage.selcloud.ru \
  RCLONE_CONFIG_BK_REGION=ru-7 RCLONE_CONFIG_BK_NO_CHECK_BUCKET=true \
  RCLONE_CONFIG_BK_ACCESS_KEY_ID=<key> RCLONE_CONFIG_BK_SECRET_ACCESS_KEY=<secret>
rclone lsf BK:eduproject/lms-production/            # выбрать дату
rclone cat BK:eduproject/lms-production/lms-backup-<TS>.tgz.sha256   # эталон суммы
rclone copyto BK:eduproject/lms-production/lms-backup-<TS>.tgz /tmp/prod-backup.tgz
shasum -a 256 /tmp/prod-backup.tgz                   # сверить вручную
# далее — как в restore-шаге deploy-preprod.yml: tar, zcat | mariadb, распаковка tars файлов
# полная БД сайта — только `bench --site … restore` / как в restore-prod.sh
```

## Безопасность и параллельность

- Локальная ротация работает только после подтверждённого S3 upload; неполный/изменённый набор не даёт удалить прежние копии. Посторонние файлы, симлинки и более новые наборы сохраняются.
- Создание и финализация сериализуются серверным `flock`; staging и metadata привязаны к архиву. S3 env-файлы уникальны для workflow run/attempt.
- Ротация S3 применяется только к каноническим именам в основном префиксе; `control-archive/` и посторонние объекты она не затрагивает.
- Используется rclone: AWS CLI v2 не проходил TLS к Selectel. Большие передачи выполняются с RU-серверов, а не напрямую раннером в Selectel.
- Восстановление управляющего Pi выполняется локально: `scripts/recover-control-local.sh [gateway|release|all]`. Production-релиз остаётся явной командой владельца с неизменяемым тегом.

### Isolated restore verification

Run `verify-production-backup.yml` to check the latest existing S3 archive without deploying or replacing the occupied preprod site. The job verifies the S3 checksum and all four embedded files, restores attachments and SQL into disposable storage on preprod, checks MariaDB tables, and verifies that both Course Lesson text fields remain Long Text. It creates no new production backup and sends no Telegram messages.

The restore uses the existing preprod database image in a separate container with no network, a read-only root filesystem, bounded memory/CPU and a reserved 3 GiB scratch filesystem. At least 768 MiB available RAM and space for the download plus 5 GiB are required before the check begins. Temporary credentials, the container and scratch storage are removed after the job; active preprod data, task ownership and images stay in place. A successful check requires the `ISOLATED_RESTORE_PASS` marker.
