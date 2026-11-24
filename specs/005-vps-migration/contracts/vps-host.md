# Контракт хоста VPS

**Дата:** 2026-11-24. Внешняя поверхность сервера и обязательства площадки перед сервисами стенда. Пользовательский API Mini App не меняется: см. [miniapp-api.md](../../004-vk-mini-app/contracts/miniapp-api.md) и [frontend-gateway.md](../../004-vk-mini-app/contracts/frontend-gateway.md). Источники решений — [research.md](../research.md) R2–R6, R9, R12.

## Сетевая поверхность

| Порт | Слушает | Доступ снаружи | Поведение |
| --- | --- | --- | --- |
| 22/tcp | sshd хоста | да | Только ключи, `PermitRootLogin no`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, под защитой fail2ban (sshd jail) |
| 80/tcp | `miniapp-gateway` | да | `/.well-known/acme-challenge/*` из `/var/www/certbot`, для остального — `301 https://miniapp.bot-alimony.ru$request_uri` |
| 443/tcp | `miniapp-gateway` | да | SNI `miniapp.bot-alimony.ru`: TLS 1.2/1.3, сертификат Let's Encrypt, HTTP/2, allowlist `miniapp-server.inc`. Другие SNI и обращения по IP — обрыв рукопожатия |
| 8080/tcp | `miniapp-gateway` на `127.0.0.1` | нет | То же содержимое без TLS, для диагностики с хоста |
| 5432, 8000 | `db`, `bot` в сети `internal` | нет | Не публикуются (`ports` отсутствуют) |

Проверка: внешний скан с ПК показывает открытыми только 22, 80 и 443 (SC-08, T025). Docker обходит ufw, поэтому `ports` в Compose добавлять запрещено: изменение этого списка требует правки контракта.

## HTTP-ответы на 443 (наблюдаемые при приёмке)

| Запрос | Ожидание |
| --- | --- |
| `GET /` | 200, `Content-Security-Policy` с `frame-ancestors https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru`, `Strict-Transport-Security`, `Cache-Control: no-store`. TTFB ≤ 1 с (SC-01) |
| `POST /api/miniapp/v1/session` без подписи | 4xx от bot |
| `/api/search`, `/api/delivered`, `/api/alerts/pending`, `/docs`, `/openapi.json` | 404 |
| превышение 10 r/s (burst 20) с одного адреса | 429 `rate_limited`. Лимит считается по TCP-адресу клиента, заголовки `CF-Connecting-IP` и `X-Forwarded-For` не учитываются |

Access log шлюза отключён (`access_log off`): адрес запуска содержит подпись.

## Запуск сервисов

Единственная форма команды на VPS (alias `dc` в quickstart):

```text
docker compose -f compose.yaml -f compose.vps.yaml --profile miniapp --profile daemon <команда>
```

- Профиль `tunnel` на VPS не включается. Разовые команды индексатора используют `--profile indexer`.
- Все долгоживущие сервисы имеют `restart: unless-stopped`, `docker.service` включён. После `reboot` состояние Up/healthy достигается за 3 минуты без ручных действий (SC-04).
- Обновление: `git pull --ff-only` → `build` → `nginx -t` → `migrate` → `up -d`. Перед миграцией, меняющей схему, выполняется внеплановая копия.

## Файловая система хоста

| Путь | Владелец / права | Назначение |
| --- | --- | --- |
| `/opt/ecoprofi` | `deploy` | Клон репозитория (deploy key, read-only) |
| `/opt/ecoprofi/.env` | `deploy`, 600 | Секреты стенда |
| `/opt/ecoprofi/out/` | `deploy`, umask 077 | Снимок переноса, сверки, локальные копии. Не в Git |
| `/opt/ecoprofi/deploy/secrets/cloudflared-token.txt` | `deploy`, 600, пустой | Только если Compose требует файл секрета (research R12) |
| `/etc/letsencrypt` | root | Сертификат, монтируется в шлюз только для чтения |
| `/var/www/certbot` | root | Webroot ACME, монтируется в шлюз только для чтения |
| `/etc/letsencrypt/renewal-hooks/deploy/reload-miniapp.sh` | root, +x | `nginx -s reload` в шлюзе после продления |
| `/etc/ecoprofi-backup.env` | root, 644 | `PROJECT_DIR`, `RCLONE_REMOTE`, `LOCAL_KEEP`. Без ключей |
| `~deploy/.config/rclone/rclone.conf` | `deploy`, 600 | Remote `ecoprofi-s3` и ключ к единственному бакету |
| `/etc/docker/daemon.json` | root | `log-driver json-file`, `max-size 10m`, `max-file 5`. При необходимости `registry-mirrors` |
| `/etc/ssh/sshd_config.d/10-hardening.conf` | root | Ограничения входа по SSH |

## Юниты systemd

| Юнит | Расписание | Действие | Результат |
| --- | --- | --- | --- |
| `docker.service` | при загрузке | поднимает контейнеры с `unless-stopped` | — |
| `certbot.timer` (пакет) | дважды в сутки | `certbot renew` через webroot | deploy-хук перезагружает шлюз |
| `ecoprofi-backup.timer` → `.service` | 03:30 МСК, `Persistent=true` | `deploy/backup.sh` от `deploy` | При успехе закрывает `backup`. При сбое `OnFailure=` открывает `backup` ([host-alerts.md](host-alerts.md)) |
| `ecoprofi-cert-check.timer` → `.service` | ежедневно 09:00 МСК, `Persistent=true` | `deploy/cert-check.sh` | Открывает или закрывает `tls` |
| `unattended-upgrades` | пакет | обновления безопасности | Перезагрузку не выполняет автоматически |

## Исходящие соединения, которые обязана разрешать площадка

`api.vk.com` и `*.vk.com` / `*.userapi.com` (Long Poll, документы, дорожки), `llm.api.cloud.yandex.net` (AI Studio), `github.com:22` (deploy key), Docker Hub или зеркало провайдера, `acme-v02.api.letsencrypt.org`, S3-эндпоинт провайдера. Проверка — T015, T016.
