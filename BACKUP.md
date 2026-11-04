# Бекапы LMS production: control + S3

## Где какой код (карта)

| Кусок | Где | Что делает |
| --- | --- | --- |
| Общий прод-бекап | `scripts/backup-production.sh` (новый) | На production: `bench backup --with-files` → `lms-backup[-label]-<TS>.tgz` (+`SHA256SUMS` внутри) → отправка на control (`backuprecv@… put …`). tgz остаётся в `/tmp` на проде для забора раннером; маркеры `BACKUP_FILE/SHA256/SIZE` |
| S3-помощник | `scripts/s3-backup.sh` (новый) | Запускается на production/preprod через ssh (rclone): `upload` (tgz + `.sha256` сайдкар, проверка размера, ротация), `download <key|latest>` (проверка sha256), `retention`. Секреты — временный env-файл `/run/tutor-s3.env` (tmpfs, 600, удаляется cleanup-шагом). Любой S3-провайдер через `S3_ENDPOINT_URL` |
| rclone на серверах | `scripts/install-rclone.sh` (новый) | rclone v1.75.2 (linux-amd64, sha256-проверка) установлен на production и preprod 2026-11-04. AWS CLI v2 не используется: его стек TLS (s2n) не соединяется с Selectel — см. `s3-net-diagnostic.yml` |
| Релиз | `.github/workflows/deploy-production.yml` | Перед выкладкой: прод-бекап (общий скрипт) → control → **upload в S3 с прода (RU→RU)** → очистка `/run/tutor-s3.env` и `/tmp` на проде |
| Клон прода на препрод | `.github/workflows/deploy-preprod.yml` (`clone_prod`) | Fresh-бекап прода (label `fresh`) → control + S3 с прода → **препрод сам скачивает из S3** (проверка восстанавливаемости по sha256-сайдкару) → restore. Без настроенного S3 — старый путь через раннер |
| Ежедневный бекап | `.github/workflows/backup-daily.yml` | 03:00 МСК ежедневно (label `daily`): тот же поток → control + S3 + ротация + журнал `releases/backups.log` |
| Приёмник на control | `tutor-control:/usr/local/bin/recv-backup.sh` | Forced-command SSH: принимает ровно `put lms-backup-*.tgz`, пишет в `/var/backups/production` (владелец `backuprecv`) |
| Ежедневный бекап control | `tutor-control:/etc/cron.d/vikunja-backup` + `/usr/local/bin/vikunja-backup.sh` | tar Vikunja+Caddy в `/var/backups/vikunja`, ротация 7 дней |

Имя файла кодирует дату и источник: `lms-backup-<TS>.tgz` (релиз), `lms-backup-fresh-<TS>.tgz` (клон на препрод), `lms-backup-daily-<TS>.tgz` (по расписанию, если включить). Ротация в S3 сортирует/чистит по дате из имени, лейблы не влияют.

## Потоки данных

```
release / daily (deploy-production.yml, backup-daily.yml):
  prod: bench backup -> /tmp/lms-backup-<TS>.tgz ──put──> control /var/backups/production
                        └──(прод сам, RU→RU)──> S3 (tgz + .sha256, проверка размера, ротация)

clone_prod (deploy-preprod.yml):
  prod: bench backup -> lms-backup-fresh-<TS>.tgz ──put──> control (старый путь, сохранён)
                        └──> S3 ──(препрод сам скачивает, sha256-проверка)──> preprod restore
```

Загрузку в S3 выполняют RU-серверы, а не раннер GitHub: канал раннер→Selectel
не пропускает ~400 МБ (36 минут и обрыв, run 41870610250), а AWS CLI v2 вдобавок
не проходит TLS к Selectel (`AWS_IO_TLS_ERROR_NEGOTIATION_FAILURE`, диагностика
run 41869442975: curl/openssl/rclone работают, aws cli v2 — нет). Раннер только
оркестрирует: передаёт секреты во временный `/run/tutor-s3.env` (tmpfs) и
запускает скрипты по ssh.

Старый путь «бекап локально на control» сохранён. Пока S3 не настроен, шаг загрузки
пишет `::warning::` и не падает; клон в этом режиме берёт проверенную копию с раннера.

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

## Ограничения и заметки

- **Диск прода (86% занято):** `bench backup` (CLI) всегда делает свежий дамп
  (`force=True`) и сам удаляет файлы старше ~23 ч (`frappe.utils.backups.delete_temp_backups`,
  `keep_backups_for_hours` по умолчанию) — накопления в контейнере нет; tgz на проде
  живёт минуты (очистка шагом workflow; чужие остатки старше часа убирает следующий запуск).
- **Control без ротации:** `/var/backups/production` уже ~15 ГБ (79 ГБ диск, 34 ГБ
  свободно). Ежедневный бекап добавляет ~12 ГБ/мес — без ротации диск кончится
  примерно через два месяца. Предложение (не включено — удаляет бекапы, нужно
  решение владельца): файл `/etc/cron.d/lms-backup-retention` на control со строкой
  `40 4 * * * root find /var/backups/production -type f -name 'lms-backup-*.tgz' -mtime +14 -delete`
- **Ежедневный бекап:** `.github/workflows/backup-daily.yml` — расписание
  `0 0 * * *` UTC = **03:00 МСК** ежедневно (МСК = UTC+3 круглый год), плюс ручной
  `workflow_dispatch`. Поток тот же: общий скрипт (label `daily`) → control →
  забор на раннер → S3 (ротация 30 дней) → очистка `/tmp` прода → строка в
  `releases/backups.log` (коммит от backup-bot; ежедневный коммит заодно не даёт
  GitHub отключить расписание после 60 дней тишины в репозитории). Для этого
  workflow пустой `S3_BUCKET` — ошибка, а не предупреждение. Задержка старта на
  несколько минут в пиковое время — норма GitHub Actions; о провале GitHub
  присылает письмо подписчикам репозитория. Telegram-уведомлений нет
  (правило: без отдельной авторизации владельца).
- Ротация в S3 чистит только `lms-backup[-label]-YYYYMMDDTHHMMSSZ.tgz` (+`.sha256`)
  внутри префикса — посторонние объекты не трогает. Канонический формат имени
  проверяется одной функцией и в upload, и в latest, и в retention, и в download;
  ошибка листинга S3 валит команду (нет молчаливых «удалено 0»).
- Контракт `s3-backup.sh`: `upload` печатает `S3_KEY=<prefix>/<имя>`; `download`
  принимает как полный ключ из этого маркера, так и само имя файла.
- Параллельность: у трёх workflow разные concurrency groups, поэтому общий
  прод-скрипт сериализует выполнение серверным локом
  `/run/lock/lms-backup-production.lock` (flock, ожидание до 10 минут) и использует
  уникальный staging-каталог на запуск (`/tmp/lms-backup-staging.*`); bench backup
  и выбор свежего сета тоже проходят под локом.
- **Почему rclone, а не AWS CLI:** диагностикой 2026-11-04 (workflow
  `s3-net-diagnostic.yml`, запуск вручную) установлено: с раннера GitHub
  curl/openssl/rclone к Selectel работают, а AWS CLI v2 (стек TLS s2n) падает
  с `AWS_IO_TLS_ERROR_NEGOTIATION_FAILURE` даже на `s3 ls`. Поэтому S3-шаги
  используют rclone; диагностический workflow оставлен для сетевых проверок.
- Для серверного шифрования/версионирования бакета настройте их на стороне провайдера
  (например, SSE-S3, versioning + lifecycle) — скрипт не мешает.
