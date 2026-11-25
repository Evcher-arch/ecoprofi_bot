# Модель данных 005: перенос стенда на VPS

**Дата:** 2026-11-24. Схема БД приложения не меняется (допущение 4 spec.md). Миграций в 005 нет. Ниже описаны эксплуатационные сущности: где они хранятся, какие у них поля и как меняется их состояние. Ссылки: [spec.md](spec.md), [research.md](research.md), [contracts/vps-host.md](contracts/vps-host.md), [contracts/host-alerts.md](contracts/host-alerts.md).

## Рабочий контур

Площадка, на которой работают писатели БД (`bot`, `vk-adapter`, `indexer-daemon`) и вход Mini App. В каждый момент она одна (FR-07).

**Фактический статус (2026-11-25):** VPS развёрнут, все сервисы работают, но перенос данных (Phase 4) не выполнен — БД на VPS пустая. Рабочий контур всё ещё на ПК.

| Поле | Значение | Где фиксируется |
| --- | --- | --- |
| площадка | `pc` \| `vps` | acceptance.md, «Площадка» |
| переключено_в | дата и время МСК | acceptance.md (T032) |
| снимок | ссылка на «Снимок переноса» | acceptance.md (T027) |
| том_возврата | `ecoprofi_db-data` на ПК, хранится до даты из решения владельца, не меньше 14 дней | acceptance.md (T050) |

**Фактическая конфигурация VPS (2026-11-25):**

| Компонент | Значение |
| --- | --- |
| VPS | 2 vCPU AMD EPYC-Milan, 2 ГБ + 2 ГБ swap, 20 ГБ NVMe |
| ОС | Ubuntu Server 24.04 LTS |
| IP | 79.174.78.44 |
| Образы | Локальная сборка → docker save → SFTP → docker load |
| Базовый образ indexer | `python:3.12-slim` (tesseract недоступен в Alpine) |
| Роли БД | `db/init/01_roles.sql` (pgvector не запускает `.sh`) |
| SSL | certbot, системный nginx остановлен |
| Сеть из контейнеров | Не резолвит внешние репозитории (OpenStack DNS) |

**Переходы состояний**

```text
pc ──T026 стоп писателей──▶ freeze ──T030 diff пуст──▶ cutover ──T031 запуск VPS, T032 стоп db ПК──▶ vps
                              │                           │
                              └──── diff ≠ ∅ / сбой ──────┴──▶ pc (up -d на ПК, VPS не запускает писателей)

vps ──возврат (T050, по решению владельца)──▶ freeze' ──свежий дамп VPS, восстановление на ПК, новый вход оператора на ПК──▶ pc
```

**Инварианты**

- В `freeze` и `cutover` писатели не работают ни на одной площадке. На ПК работает только `db`, на VPS — только `db` до T031.
- В `vps` на ПК не выполняются bot, vk-adapter, indexer-daemon, tunnel, gateway и разовые команды индексатора (research R8). Автозапуск контейнеров ПК снят.
- Пара доступа оператора не копируется между площадками ни в одну сторону.

## Снимок переноса

Дамп БД ПК в момент `freeze`.

| Поле | Тип / формат | Правило |
| --- | --- | --- |
| файл | `out/ecoprofi-migration.dump`, `pg_dump -Fc` | Только в `out/` (исключён из Git). Содержит закрытые данные |
| sha256 | hex | Совпадает на ПК (T027) и VPS (T028) |
| размер | байты | Записывается в acceptance.md |
| counts | `out/counts-pc.txt`, строки `таблица\|число` и `migration\|имя` | Формируется `deploy/table-counts.sql` внутри контейнера |
| версии | `version()`, `vector.extversion` на ПК и VPS | research R7 |

**Валидация:** `diff out/counts-pc.txt out/counts-vps.txt` пуст (SC-02). Четыре контрольных вопроса (эпизод, презентация, письмо, запись без текста) дают те же источники (T039). При несовпадении переход в `vps` запрещён.

Перенос не меняет состояние заданий: ошибочные материалы и незавершённые проходы остаются в прежних статусах (next-stages §3 п. 1). Незавершённые попытки Mini App восстанавливает обработчик 004 при запуске bot.

## Резервная копия

Ночной дамп VPS вне сервера (FR-09).

| Поле | Формат | Правило |
| --- | --- | --- |
| имя | `ecoprofi-YYYY-MM-DD_HHMM.dump` (+ `.sha256`) | Время по МСК (TZ хоста) |
| место | `$RCLONE_REMOTE/daily/`, по воскресеньям ещё `weekly/` | Удаление старше 8 и 29 дней. Локально в `out/backups/` последние `LOCAL_KEEP` |
| проверка | `pg_restore --list` прочитал оглавление | Без проверки файл не публикуется (`.part`) |
| пробное восстановление | дата, длительность, давность копии, результат diff | acceptance.md (T048, SC-05) |

**Переходы:** `.part` → проверен → загружен в daily (и weekly) → удалён по ротации. Сбой на любом шаге даёт ненулевой код юнита и открывает оповещение `backup`. Успешный запуск его закрывает (contracts/host-alerts.md).

## Сертификат

| Поле | Значение |
| --- | --- |
| имя | `miniapp.bot-alimony.ru` (`/etc/letsencrypt/live/...`) |
| способ продления | `standalone` до T024, затем `webroot` `/var/www/certbot` |
| deploy-хук | `/etc/letsencrypt/renewal-hooks/deploy/reload-miniapp.sh` |
| порог оповещения | 14 дней до `notAfter` → причина `tls` |

## Оповещения хоста

Хранятся в существующей таблице `alert` (миграция `003_autonomy.sql`) без изменения схемы. Используемые поля: `cause_key`, `kind='action'`, `body`, `state`, `opened_at`, `closed_at`. Уникальность: одна запись `state='open'` на `cause_key` (`alert_open_idx`). Новые значения `cause_key`: `tls` и `backup`. Правила открытия и закрытия описаны в [contracts/host-alerts.md](contracts/host-alerts.md). Отправку отмечает bot (`sent_count`, `last_sent_at`, `recovery_sent`), как для причин 003.

## Секреты и их размещение

| Секрет | На VPS | Права | Не допускается |
| --- | --- | --- | --- |
| `.env` (БД, VK, Yandex, Mini App) | `/opt/ecoprofi/.env` | 600 `deploy` | Git, чат, логи, acceptance.md |
| пара доступа оператора | том `ecoprofi_indexer-secrets`, получен новым входом | том Docker | копирование с ПК и на ПК |
| ключ S3 | `~deploy/.config/rclone/rclone.conf` | 600 | `/etc/ecoprofi-backup.env`, Git |
| deploy key GitHub | `~deploy/.ssh/github_deploy` | 600, read-only в GitHub | права записи |
| токен Cloudflare | не переносится | — | на VPS |
