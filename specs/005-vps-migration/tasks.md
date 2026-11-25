---
description: "Задачи 005: перенос стенда на VPS"
---

# Задачи 005: перенос стенда на VPS

**Вход:** [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md).

**Дата:** 2026-11-24 (перестроено по plan.md, нумерация изменена). **Статус:** 14/55. Подготовка репозитория T001–T005 и скрипты оповещений хоста T041–T045 выполнены (2026-11-24). Единый скрипт деплоя deploy/vps-deploy.py создан (2026-11-25). Фактический деплой на VPS 79.174.78.44 выполнен 2026-11-25: образы собраны локально, загружены через docker save/load, роли БД созданы через db/init/01_roles.sql, миграции применены, SSL-сертификат выпущен, все 5 сервисов работают (bot, db, indexer-daemon, vk-adapter, miniapp-gateway). HTTPS miniapp.bot-alimony.ru → 200 OK.

**Обозначения.** `[P]` — можно выполнять параллельно с соседними задачами (другие файлы или другая площадка, нет зависимости от незавершённых задач). `[US1]`–`[US3]` — история spec.md. **[Ч]** — выполняет человек (владелец или оператор) по шагам. **[Р]** — изменение репозитория, выполняет разработчик или Claude. `[ПК]` — PowerShell на текущем ПК из папки проекта. `[VPS]` — bash на сервере под пользователем `deploy` в `/opt/ecoprofi`. `dc` — alias из quickstart: `docker compose -f compose.yaml -f compose.vps.yaml --profile miniapp --profile daemon`. Результат каждой задачи **[Ч]** записывается в `specs/005-vps-migration/acceptance.md`: дата, факт, отклонения. Секреты в протокол не вносятся.

---

## Phase 1: Setup — подготовка репозитория и сайзинг

**Назначение:** файлы развёртывания и исходные данные для заказа сервера.

- [x] T001 [Р] Оверлей `compose.vps.yaml`: шлюз публикует `80:80` и `443:443` в дополнение к диагностическому `127.0.0.1:8080`, монтирует `deploy/vps/miniapp-nginx.conf` как `default.conf`, `/etc/letsencrypt` (ro) и `/var/www/certbot` (ro). Профиль `tunnel` на сервере не используется.
- [x] T002 [Р] `deploy/vps/miniapp-nginx.conf`. Порт 80: `/.well-known/acme-challenge/` из webroot, остальное — 301 на https. Порт 443: сертификат Let's Encrypt и тот же `miniapp-server.inc` (CSP, HSTS, allowlist), отказ рукопожатия для чужих имён и обращений по IP. Порт 8080 — для диагностики. `miniapp-realip.inc` не подключается: доверия `CF-Connecting-IP` нет. Образ шлюза и конфиг для ПК не меняются.
- [x] T003 [Р] `deploy/backup.sh`: `pg_dump -Fc` → проверка `pg_restore --list` → sha256 → rclone в S3 `daily/` и `weekly/` (воскресенье) → ротация 7 дней и 4 недели, локально 3 копии. При ошибке ненулевой код возврата. Unit-файлы `deploy/systemd/ecoprofi-backup.{service,timer}` на 03:30 МСК.
- [x] T004 [P] [Р] `deploy/table-counts.sql`: точные количества строк всех таблиц `public` (через `query_to_xml`) и список `schema_migrations`.
- [x] T005 [Р] `specs/005-vps-migration/quickstart.md` с командами и `acceptance.md` с пустой матрицей. Коммит в `master`. Push выполняется до T017.
- [ ] T006 [Ч] `[ПК]` Снять фактическое потребление для сайзинга: `docker stats --no-stream`, `docker system df -v`, размер БД `docker compose exec -T db psql -U ecoprofi_owner -d ecoprofi -tAc "SELECT pg_size_pretty(pg_database_size('ecoprofi'));"`. Записать в `specs/005-vps-migration/acceptance.md` («Площадка»). Если том БД больше 10 ГБ, диск берётся не меньше 5-кратного объёма тома плюс 30 ГБ (spec.md, «Сайзинг»).

**Checkpoint:** репозиторий содержит файлы развёртывания, тариф выбран.

---

## Phase 2: Foundational — сервер, домен, сертификат, сборка

**Назначение:** базовая площадка, без которой не выполняется ни одна история.

**⚠️ CRITICAL:** до конца фазы на VPS не запускаются bot, vk-adapter и indexer-daemon. ПК остаётся рабочим контуром.

- [ ] T007 [Ч] Заказать VPS по research R1: российский ЦОД (Москва/СПб), KVM, **4 vCPU / 8 ГБ RAM / 80 ГБ NVMe** (минимум 2/4/40, с поправкой T006), статический IPv4, **Ubuntu Server 24.04 LTS**. При заказе добавить свой публичный SSH-ключ. Включить снимки провайдера и создать S3-бакет (10–20 ГБ) с отдельным ключом доступа только к нему. Записать провайдера, тариф и IP в `specs/005-vps-migration/acceptance.md` (IP — не секрет).
- [ ] T008 [P] [Ч] `[ПК]` Подготовка зоны к смене NS (research R5). Начинать сразу после T007:
  1. В Cloudflare → DNS → Records выписать **все** записи зоны, кроме `miniapp`: MX, TXT/SPF/DKIM, CAA, прочие A/CNAME. Если есть CAA, в новой зоне должна остаться запись `0 issue "letsencrypt.org"`.
  2. Проверить DNSSEC: `Resolve-DnsName bot-alimony.ru -Type DS -Server 8.8.8.8`. Если DS есть, отключить DNSSEC в Cloudflare (DNS → Settings), удалить DS в REG.RU (домен → DNSSEC) и дождаться пустого ответа на тот же запрос. Только после этого выполнять T009.
  3. Записать в `specs/005-vps-migration/acceptance.md`: был ли DNSSEC, перенесённые типы записей (без значений DKIM).
- [ ] T009 [Ч] `[ПК]` Смена NS на REG.RU (решение владельца 2026-11-24, зависит от T008). Смена расходится до 24–48 часов.
  1. REG.RU → домен `bot-alimony.ru` → «DNS-серверы и управление зоной» → «Использовать DNS-серверы REG.RU» (`ns1.reg.ru`, `ns2.reg.ru`).
  2. В зоне REG.RU создать записи из T008 и `A miniapp → <IP VPS>` с TTL 300.
  3. В Cloudflare удалить туннель `ecoprofi-miniapp` (Networks → Tunnels), затем удалить сайт из аккаунта.
  4. Проверка: `Resolve-DnsName bot-alimony.ru -Type NS -Server 8.8.8.8` → `ns*.reg.ru`. `Resolve-DnsName miniapp.bot-alimony.ru -Server 8.8.8.8` и `-Server 77.88.8.8` → IP VPS. Пока ответы не совпадут, T018 не выполнять.
- [ ] T010 [P] [Ч] `[VPS]` Первый вход и пользователь `deploy` (`/home/deploy/.ssh/authorized_keys`):
  ```bash
  ssh root@<IP>
  adduser deploy && usermod -aG sudo deploy
  mkdir -p /home/deploy/.ssh && cp ~/.ssh/authorized_keys /home/deploy/.ssh/ && chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
  ```
  Проверить вход `ssh deploy@<IP>` в **новом** окне, не закрывая старое.
- [ ] T011 [Ч] `[VPS]` Закрыть SSH: в `/etc/ssh/sshd_config.d/10-hardening.conf` указать `PermitRootLogin no`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`. Затем `sudo systemctl restart ssh` и повторная проверка входа под `deploy` в новом окне.
- [ ] T012 [Ч] `[VPS]` Обновления, время, swap, fail2ban (`/etc/fstab`, `/swapfile`):
  ```bash
  sudo apt update && sudo apt full-upgrade -y
  sudo apt install -y unattended-upgrades fail2ban ufw git curl ca-certificates rclone
  sudo dpkg-reconfigure -plow unattended-upgrades
  sudo timedatectl set-timezone Europe/Moscow && timedatectl   # NTP synchronized: yes
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  sudo systemctl enable --now fail2ban && sudo fail2ban-client status sshd   # jail sshd активен
  ```
- [ ] T013 [Ч] `[VPS]` Брандмауэр: `sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable`. Docker публикует порты в обход ufw, поэтому наружу в Compose публикуются только 80/443 шлюза (contracts/vps-host.md, проверка — T025).
- [ ] T014 [Ч] `[VPS]` Docker из официального репозитория (`/etc/apt/sources.list.d/docker.list`):
  ```bash
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list
  sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker deploy && sudo systemctl enable docker
  ```
  Перелогиниться и проверить: `docker version`, `docker compose version`.
- [ ] T015 [Ч] `[VPS]` `/etc/docker/daemon.json` и реестр (research R6). Создать файл с ротацией журналов (quickstart, «Журналы Docker и зеркало реестра»): `"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "5"}`. Затем `sudo systemctl restart docker`. Проверить доступ к реестру: `docker pull python:3.12-slim node:24.14.1-bookworm-slim nginx:1.29.3-alpine pgvector/pgvector:pg16`. При ошибке добавить в тот же файл `"registry-mirrors": [...]` провайдера, перезапустить Docker и повторить. Записать в acceptance.md, какой путь сработал.
- [ ] T016 [P] [Ч] `[VPS]` Исходящий доступ (contracts/vps-host.md, «Исходящие соединения»): `curl -sSI https://api.vk.com/method/`, `curl -sSI https://llm.api.cloud.yandex.net`, `curl -sSI https://github.com`, `curl -sSI https://acme-v02.api.letsencrypt.org/directory`, S3-эндпоинт провайдера. Нужны любые HTTP-ответы, а не тайм-ауты. Результат записать в acceptance.md.
- [ ] T017 [Ч] `[VPS]` Получить код в `/opt/ecoprofi`. Перед этим на ПК выполнить push коммитов `master`, включая plan.md и этот tasks.md.
  ```bash
  sudo mkdir -p /opt/ecoprofi && sudo chown deploy:deploy /opt/ecoprofi
  ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""   # публичную часть добавить в GitHub → Settings → Deploy keys (read-only)
  GIT_SSH_COMMAND="ssh -i ~/.ssh/github_deploy" git clone git@github.com:Evcher-arch/ecoprofi_bot.git /opt/ecoprofi
  cd /opt/ecoprofi && git config core.sshCommand "ssh -i ~/.ssh/github_deploy" && git log -1 --oneline
  ```
  Коммит должен совпадать с последним push. Если GitHub недоступен, передать архив по scp (допущение 3) и записать это.
- [ ] T018 [Ч] `[VPS]` Выпустить сертификат до запуска шлюза, пока порт 80 свободен (зависит от T009):
  ```bash
  sudo apt install -y certbot
  sudo certbot certonly --standalone -d miniapp.bot-alimony.ru -m <почта владельца> --agree-tos --no-eff-email
  sudo mkdir -p /var/www/certbot
  ```
  Создать хук `/etc/letsencrypt/renewal-hooks/deploy/reload-miniapp.sh` (quickstart, «Сертификат»). Переключение продления на webroot выполняется в T024.
- [ ] T019 [Ч] `[ПК]`→`[VPS]` Секреты только по SSH: `scp .env deploy@<IP>:/opt/ecoprofi/.env`, затем на VPS `chmod 600 /opt/ecoprofi/.env`. Проверить `MINIAPP_PUBLIC_ORIGIN=https://miniapp.bot-alimony.ru` и `MINIAPP_ENABLED=true`. Строки и токены в чат и протокол не копировать. `deploy/secrets/cloudflared-token.txt` на сервер **не** переносить.
- [ ] T020 [Ч] `[VPS]` Проверка проекта и сборка (research R12):
  1. `dc config -q`. Если Compose требует файл секрета `miniapp_tunnel_token`, создать пустую заглушку `install -D -m 600 /dev/null deploy/secrets/cloudflared-token.txt` и повторить. Записать в acceptance.md, понадобилась ли заглушка.
  2. `dc build`.
  3. `dc run --rm --no-deps --entrypoint nginx miniapp-gateway -t` → `syntax is ok`, `test is successful`.
- [ ] T021 [Ч] `[VPS]` Автоматические проверки на сервере: `docker compose --profile test run --rm --build tests`. Сравнить число пройденных и пропущенных тестов с последним прогоном на ПК и записать в acceptance.md.
- [ ] T022 [Ч] `[VPS]` Поднять только `db`: `docker compose up -d db`, дождаться `healthy` в `docker compose ps`. Init-скрипт `db/init/01_roles.sh` создаёт роли `ecoprofi_bot`, `ecoprofi_indexer` и расширение `vector`. Других сервисов, кроме шлюза в T023, не запускать.

**Checkpoint:** сервер защищён, домен указывает на VPS, сертификат выпущен, образы собраны, тесты пройдены, пустая БД работает.

---

## Phase 3: User Story 1 — участник открывает Mini App с сервера (P1), часть 1: вход 🎯 MVP

**Цель:** публичный вход `https://miniapp.bot-alimony.ru` работает с VPS без Cloudflare из сетей участников. Проверяется до переключения данных.

**Независимая проверка:** шлюз запущен без bot (`--no-deps`). `GET /` отдаёт 200 с CSP и HSTS из трёх мобильных сетей и одной домашней, `http` перенаправляет на `https`, снаружи открыты только 22/80/443, `certbot renew --dry-run` успешен. API на этом шаге отвечает 502: это ожидаемо, bot ещё не запущен, а шлюз разрешает имя `bot` при запросе (`deploy/miniapp-proxy.inc`).

- [ ] T023 [US1] [Ч] `[VPS]`+`[ПК]` Запустить только шлюз: `dc up -d --no-deps miniapp-gateway`. С ПК: `curl.exe -sSI https://miniapp.bot-alimony.ru/` → 200, `Content-Security-Policy` с `frame-ancestors https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru`, `Strict-Transport-Security`. `curl.exe -sSI http://miniapp.bot-alimony.ru/` → 301 на https. `curl.exe -s -o NUL -w "%{time_starttransfer}" https://miniapp.bot-alimony.ru/` → не больше 1 с. Обращение по IP (`curl.exe -skI https://<IP>/`) → обрыв рукопожатия. Открыть адрес с телефона в сетях **трёх разных мобильных операторов** и одного домашнего провайдера (SC-01). В `specs/005-vps-migration/acceptance.md` («Вход из сетей») записать оператора, код, время загрузки и отсутствие ошибок сертификата.
- [ ] T024 [P] [US1] [Ч] `[VPS]` Продление сертификата через webroot (research R4): `sudo certbot reconfigure --cert-name miniapp.bot-alimony.ru --webroot -w /var/www/certbot`, затем `sudo certbot renew --dry-run` — успешно (SC-07). `systemctl list-timers certbot` показывает таймер. Дополнительно — внешний тест SSL (например, SSL Labs) с оценкой не ниже A. Записать в acceptance.md.
- [ ] T025 [P] [US1] [Ч] `[ПК]` Внешняя поверхность (contracts/vps-host.md): `Test-NetConnection <IP> -Port 5432`, `-Port 8000`, `-Port 8080` → `TcpTestSucceeded: False`. `-Port 22`, `-Port 80`, `-Port 443` → `True`. На VPS `sudo ss -tlnp` показывает 8080 только на `127.0.0.1`. Записать в acceptance.md (SC-08, часть «порты»).

**Checkpoint:** вход с сервера подтверждён до переключения. Если T023 не проходит из какой-либо сети, переключение (Phase 4) не начинается, ПК остаётся рабочим контуром.

---

## Phase 4: User Story 2 — данные и операторский доступ перенесены без потерь (P1)

**Цель:** каталог, версии, аудитории, решения редактора, очередь и незавершённые проходы на VPS совпадают со снимком ПК. Работает один рабочий контур. Оператор заново вошёл на сервере, индексатор выполняет плановые проходы.

**Независимая проверка:** `diff out/counts-pc.txt out/counts-vps.txt` пуст (SC-02, количества). На ПК не работают писатели и туннель. `indexer status` на VPS показывает действующий доступ и следующий проход в 07:00 или 19:00 МСК. Тестовое оповещение дошло оператору.

Окно работ — около 1 часа, вне плановых проходов 07:00/19:00 МСК. Участников предупредить заранее. Переходы состояний контура описаны в data-model.md («Рабочий контур»).

- [ ] T026 [US2] [Ч] `[ПК]` Перевод в `freeze`. Убедиться, что индексатор не выполняет проход: `docker compose --profile indexer run --rm indexer python -m ecoprofi.indexer status`. Остановить писателей и вход: `docker compose --profile miniapp --profile tunnel --profile daemon stop vk-adapter indexer-daemon bot miniapp-tunnel miniapp-gateway`. `db` оставить работать. **Не выполнять `down -v`.** Время остановки записать в acceptance.md.
- [ ] T027 [US2] [Ч] `[ПК]` Снимок переноса и контрольные данные в `out/` (не в Git):
  ```powershell
  New-Item -ItemType Directory -Force out | Out-Null
  docker compose exec -T db pg_dump -U ecoprofi_owner -d ecoprofi -Fc -f /tmp/ecoprofi.dump
  docker compose cp db:/tmp/ecoprofi.dump out/ecoprofi-migration.dump
  Get-FileHash out/ecoprofi-migration.dump -Algorithm SHA256
  docker compose cp deploy/table-counts.sql db:/tmp/counts.sql
  docker compose exec -T db sh -c "psql -U ecoprofi_owner -d ecoprofi -tA -f /tmp/counts.sql > /tmp/counts.txt"
  docker compose cp db:/tmp/counts.txt out/counts-pc.txt
  docker compose exec -T db psql -U ecoprofi_owner -d ecoprofi -tAc "SELECT version(); SELECT extversion FROM pg_extension WHERE extname='vector';"
  ```
  Файл сверки формируется внутри контейнера, поэтому кодировка PowerShell не портит `diff` на VPS. В acceptance.md записать SHA256, размер, версии PostgreSQL и pgvector (research R7). Дамп содержит закрытые данные.
- [ ] T028 [US2] [Ч] Передача: `[VPS]` `mkdir -p /opt/ecoprofi/out && chmod 700 /opt/ecoprofi/out`. `[ПК]` `scp out/ecoprofi-migration.dump out/counts-pc.txt deploy@<IP>:/opt/ecoprofi/out/`. `[VPS]` `sha256sum out/ecoprofi-migration.dump` — хэш совпадает с T027.
- [ ] T029 [US2] [Ч] `[VPS]` Восстановление в БД из T022:
  ```bash
  docker compose cp out/ecoprofi-migration.dump db:/tmp/ecoprofi.dump
  docker compose exec -T db pg_restore -U ecoprofi_owner -d ecoprofi --exit-on-error /tmp/ecoprofi.dump
  docker compose run --rm bot python -m ecoprofi.bot.migrate      # ожидается: новых миграций нет
  docker compose exec -T db vacuumdb -U ecoprofi_owner -d ecoprofi --analyze-only
  ```
  Статистика планировщика не переносится `pg_restore` (research R7). Если восстановление падает на существующем объекте, не продолжать: разобрать причину, ПК остаётся рабочим контуром.
- [ ] T030 [US2] [Ч] `[VPS]` Сверка — точка невозврата: `docker compose exec -T db psql -U ecoprofi_owner -d ecoprofi -tA < deploy/table-counts.sql > out/counts-vps.txt && diff out/counts-pc.txt out/counts-vps.txt`. Пустой `diff` выполняет SC-02 в части количеств. Записать версии PostgreSQL и pgvector на VPS (quickstart, «Сверка количеств»). При расхождении не продолжать: ПК остаётся рабочим контуром, сервисы ПК возвращаются командой `docker compose --profile miniapp --profile daemon up -d`.
- [ ] T031 [US2] [Ч] `[VPS]` Запуск сервисов (`cutover` → `vps`): `dc up -d bot vk-adapter miniapp-gateway`, затем `dc ps` — все Up. `indexer-daemon` пока не запускать (T035).
- [ ] T032 [US2] [Ч] `[ПК]` Зафиксировать контур (FR-07, research R8): `docker compose stop db`, затем `docker update --restart=no $(docker ps -aq --filter label=com.docker.compose.project=ecoprofi)`. Записать время переключения в acceptance.md («Площадка»). Том `ecoprofi_db-data` на ПК хранится не меньше 14 дней. **После этого на ПК не выполняются никакие команды индексатора** (включая `status`): они продлили бы прежнюю пару доступа на второй площадке.
- [ ] T033 [US2] [Ч] `[VPS]` Новый вход оператора: `docker compose --profile indexer run --rm indexer python -m ecoprofi.indexer access login`. Открыть выданный адрес VK ID у себя в браузере, адрес `https://localhost/callback?...` вставить обратно в CLI (страница не загрузится, это нормально). Затем `docker compose --profile indexer run --rm indexer python -m ecoprofi.indexer status`. Том `indexer-secrets` с ПК не копировать.
- [ ] T034 [US2] [Ч] `[VPS]` Разовая проверка чтения на сервере (OPS-01): расписание, платная лента, документ, дорожка — командами индексатора из `specs/003-autonomous-indexer/quickstart.md`. Записать исходы в acceptance.md. Отказ доступа, пустой ответ, сетевой сбой и ошибка разбора должны различаться (NET-01).
- [ ] T035 [US2] [Ч] `[VPS]` Плановый режим: `dc up -d indexer-daemon`. По `indexer status` убедиться, что следующий проход назначен на 07:00 или 19:00 МСК и активный проход один.
- [ ] T036 [US2] [Ч] Оповещения с VPS: убедиться, что тестовое или реальное оповещение по правилам `specs/003-autonomous-indexer/contracts/alerts.md` дошло оператору (`ALERT_VK_USER_ID`). Записать в acceptance.md.

**Checkpoint:** VPS — единственный рабочий контур, данные сверены, индексатор работает по расписанию.

---

## Phase 5: User Story 1 — участник открывает Mini App с сервера (P1), часть 2: живая матрица

**Цель:** вход, подписка, вопрос, уточнение и переход к источнику работают с сервера так же, как на локальном стенде. Это перенесённые T028, T042, T055, T058–T060 функции 004.

**Независимая проверка:** матрица SC-01–SC-08 функции 004 заполнена серверными свидетельствами на телефоне и ПК (SC-03). Неподписчик получает только пояснение и ссылку клуба. Зависит от Phase 4: живые ответы требуют перенесённых данных.

- [ ] T037 [US1] [Ч] `[ПК]`+`[VPS]` Allowlist и секреты (SC-08). `curl.exe -si -X POST https://miniapp.bot-alimony.ru/api/miniapp/v1/session` без подписи → 4xx от bot. `/api/search`, `/api/delivered`, `/api/alerts/pending`, `/docs`, `/openapi.json` → 404. Больше 30 запросов подряд с одного адреса → 429 `rate_limited`. Секреты в журналах: `dc logs --no-color | grep -Ei "sign=|token|secret|bearer|password"` — совпадений с реальными значениями нет. В acceptance.md записать проверенные шаблоны, без значений.
- [ ] T038 [US1] [Ч] Плитка «Найти лекцию» в сообществе «Экопрофи» на телефоне и ПК: вход без ручного ввода ID и пароля, роли «подписчик», «неподписчик» и «исключение» (`CLUB_EXEMPT_IDS`). Неподписчик получает только пояснение и ссылку клуба, поиск не вызывается (бывш. T028 004). Записать в acceptance.md («Живая матрица 004»).
- [ ] T039 [US1] [Ч] Вопрос → уточнение → ответ → источник для эпизода, презентации, письма и записи без текста. Сверить интервал, страницу и реквизиты с оригиналом. Проверить отсутствие сообщений у администрации (бывш. T042 004). Четыре контрольных вопроса должны дать те же источники, что на ПК (SC-02, контрольные вопросы).
- [ ] T040 [US1] [Ч] Две учётные записи × два диалога, перезагрузка вкладки, смена учётной записи. `dc stop bot` / `dc start bot` при открытом чате: показывается ошибка, а не «не найдено» (бывш. T055, T059 004). Заполнить матрицу SC-01–SC-08 функции 004 серверными свидетельствами в acceptance.md (SC-03).

**Checkpoint:** истории 1 и 2 выполнены, Mini App работает с сервера на реальных данных.

---

## Phase 6: User Story 3 — сервер переживает сбой и восстанавливается из копии (P2)

**Цель:** после перезагрузки стенд поднимается сам. Ночная копия хранится вне VPS и проверена восстановлением. Сбой копии или приближение срока сертификата приходит оператору оповещением. Возврат на ПК описан и отрепетирован.

**Независимая проверка:** `sudo reboot` → все сервисы Up/healthy за 3 минуты (SC-04). Пробное восстановление из S3 в изолированный контейнер даёт совпадение количеств (SC-05). `host-alert.sh open/close tls` даёт оператору сообщения об открытии и восстановлении.

Задачи [Р] T041–T045 не зависят от сервера и могут выполняться параллельно с Phase 2–5. Установка на VPS (T046–T047) — после T031.

- [x] T041 [P] [US3] [Р] `deploy/host-alert.sh` по contracts/host-alerts.md. Bash, `set -euo pipefail`. Интерфейс: `host-alert.sh open <причина> <текст>` | `host-alert.sh close <причина>`. `<причина>` допускает только `tls` и `backup`, иначе код 2. Скрипт выполняет `cd "${PROJECT_DIR:-/opt/ecoprofi}"` и пишет через `docker compose -f compose.yaml exec -T db psql -U ecoprofi_owner -d ecoprofi -v ON_ERROR_STOP=1 -v cause=... -v body=...`. Значения передаются переменными psql (`:'cause'`, `:'body'`), а не подстановкой в текст SQL. `open` — один оператор, как `open_alert` 003: `INSERT INTO alert (cause_key, kind, body) VALUES (:'cause', 'action', :'body') ON CONFLICT (cause_key) WHERE state = 'open' DO UPDATE SET body = excluded.body`. `close` — `UPDATE alert SET state='closed', closed_at=now(), recovery_sent = (sent_count = 0) WHERE cause_key=:'cause' AND state='open'` (как `close_alert` 003). Если БД недоступна — ненулевой код и сообщение в stderr. Права `+x` (`git update-index --chmod=+x`).
- [x] T042 [P] [US3] [Р] `deploy/cert-check.sh`. Bash, запуск от root. Читает `/etc/letsencrypt/live/miniapp.bot-alimony.ru/fullchain.pem`, порог `CERT_WARN_DAYS` (по умолчанию 14). Если `openssl x509 -checkend $((CERT_WARN_DAYS*86400)) -noout -in <файл>` успешен, выполняется `host-alert.sh close tls`. Иначе — `host-alert.sh open tls` с текстом по правилам 003: первая строка `Сертификат miniapp.bot-alimony.ru истекает через N дн. (<notAfter> МСК).`, вторая `Выполнить: sudo certbot renew; sudo certbot certificates.`. Если файл не читается, открывается `tls` с текстом «Сертификат miniapp.bot-alimony.ru не читается». Секретов и путей ключей в тексте нет. Права `+x`.
- [x] T043 [P] [US3] [Р] Юниты в `deploy/systemd/`. `ecoprofi-cert-check.service`: `Type=oneshot`, `Requires=docker.service`, `After=docker.service`, `ExecStart=/opt/ecoprofi/deploy/cert-check.sh`. `ecoprofi-cert-check.timer`: `OnCalendar=*-*-* 09:00:00 Europe/Moscow`, `Persistent=true`, `WantedBy=timers.target`. `ecoprofi-alert-backup.service`: `Type=oneshot`, `User=deploy`, `Group=docker`, `ExecStart=/opt/ecoprofi/deploy/host-alert.sh open backup "Ночная резервная копия БД не создана. Выполнить: journalctl -u ecoprofi-backup -n 50; sudo systemctl start ecoprofi-backup.service"`. Время сбоя в текст не подставляется: момент открытия хранится в `alert.opened_at`, шаг сбоя виден в журнале юнита.
- [x] T044 [US3] [Р] Подключить оповещение `backup` (зависит от T041). В `deploy/systemd/ecoprofi-backup.service` в секцию `[Unit]` добавить `OnFailure=ecoprofi-alert-backup.service`. В `deploy/backup.sh` последним шагом после ротации и перед итоговым `echo` добавить `"$PROJECT_DIR/deploy/host-alert.sh" close backup || echo "warning: не удалось закрыть оповещение backup" >&2`. Сбой закрытия не делает копию неуспешной.
- [ ] T045 [US3] [Р] Сверить скрипты T041–T044 с contracts/host-alerts.md и `bash -n` (на ПК — через Git Bash). Обновить таблицы «Файлы развёртывания» в `specs/005-vps-migration/quickstart.md` (новые файлы) и снять пометку «план» в разделе «Оповещения хоста». Коммит в `master`, push по согласованию владельца. На VPS — `git pull --ff-only` в `/opt/ecoprofi`.
- [ ] T046 [US3] [Ч] `[VPS]` Резервные копии (quickstart, «Резервная копия», FR-09): создать `/etc/ecoprofi-backup.env` с `PROJECT_DIR`, `RCLONE_REMOTE`, `LOCAL_KEEP`, затем `rclone config` (remote `ecoprofi-s3`, ключ только в `~/.config/rclone/rclone.conf`, права 600). `sudo cp deploy/systemd/ecoprofi-backup.* deploy/systemd/ecoprofi-alert-backup.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now ecoprofi-backup.timer`. Разово запустить `sudo systemctl start ecoprofi-backup.service`, проверить `rclone ls ecoprofi-s3:<бакет>/daily/` и записать в acceptance.md.
- [ ] T047 [US3] [Ч] `[VPS]` Оповещения хоста (contracts/host-alerts.md, «Проверка»): `sudo cp deploy/systemd/ecoprofi-cert-check.* /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now ecoprofi-cert-check.timer`.
  1. `deploy/host-alert.sh open tls "Проверка канала оповещений хоста"` → сообщение оператору в течение минуты. `deploy/host-alert.sh close tls` → одно сообщение «Восстановлено».
  2. `sudo CERT_WARN_DAYS=90 deploy/cert-check.sh` открывает `tls`, `sudo deploy/cert-check.sh` закрывает.
  3. Временно задать в `/etc/ecoprofi-backup.env` неверный `RCLONE_REMOTE` → `sudo systemctl start ecoprofi-backup.service` завершается ошибкой → открывается `backup`. Вернуть значение, повторить запуск → причина закрыта.
  Записать исходы в acceptance.md.
- [ ] T048 [US3] [Ч] `[VPS]` Пробное восстановление (SC-05, quickstart «Резервная копия»): скачать последнюю копию из S3 и поднять **изолированную** БД (`docker run -d --name restore-check ... pgvector/pgvector:pg16`, без сети `internal`, без bot и индексатора). Восстановить, прогнать `deploy/table-counts.sql`, сравнить с текущей БД. Записать длительность восстановления и давность копии. Удалить контейнер и `out/restore`. Восстановленная копия сама не запускает опросы, продление доступа и сообщения.
- [ ] T049 [US3] [Ч] `[VPS]` Перезагрузка (SC-04): `sudo reboot`. Через 3 минуты: `dc ps` — все Up/healthy, `curl -sSI https://miniapp.bot-alimony.ru/` → 200, `systemctl list-timers ecoprofi-backup ecoprofi-cert-check certbot` — таймеры активны, `indexer status` — проход по расписанию не сбит.
- [ ] T050 [US3] [Ч] Репетиция возврата без реального переключения: описать в `specs/005-vps-migration/acceptance.md` («Возврат на ПК») шаги возврата. Остановить писателей на VPS → свежий дамп VPS → обновить образ `pgvector/pgvector:pg16` на ПК, если версия ниже VPS (research R7) → восстановление на ПК → A-запись на прежний вход → **новый вход оператора на ПК**, пару с VPS не копировать → запуск ПК. Проверить, что том ПК цел: `docker volume inspect ecoprofi_db-data`. Записать решение владельца, когда удалить том ПК после 14 дней.

**Checkpoint:** все три истории выполнены.

---

## Phase 7: Polish — наблюдение, документы, итоговая приёмка

**Назначение:** сквозные обязательства next-stages §3 и раздела «Порядок изменений» конституции.

- [ ] T051 [P] [Ч] `[VPS]` Доступ оператора на сервере (next-stages §3 п. 2, research R8). За время наблюдения T054 фиксировать в acceptance.md по `indexer status` хотя бы одно автоматическое продление доступа. В согласованный владельцем момент отрепетировать отзыв (отзыв приложения в настройках VK ID оператора) → оповещение `access` → повторный `access login` → успешный проход. Записать даты и исходы.
- [ ] T052 [P] [Ч] Обновить раздел развёртывания в `Архитектура_бота_Экопрофи.docx` (research R13, конституция «Порядок изменений»): вход — прямой HTTPS на VPS с Let's Encrypt вместо Cloudflare Tunnel, площадка — VPS в РФ, резервные копии в S3, оповещения хоста `tls` и `backup`, критерий приёмки — SC-01–SC-08 этой спеки. Текст для вставки готовит разработчик по plan.md, вносит и подтверждает владелец.
- [ ] T053 [P] [Ч] Решение владельца о внешнем мониторинге доступности (research R11): подключить или принять ограничение «при полной недоступности VPS оповещения нет». Записать решение в acceptance.md.
- [ ] T054 [Ч] Семь суток при выключенном ПК (SC-06): ежедневно записывать в acceptance.md («Наблюдение 7 суток») исходы проходов 07:00/19:00, число автоматических опросов (не больше двух в сутки), оповещения, наличие свежей ночной копии и вмешательства.
- [ ] T055 [Ч] Итоговая приёмка: сверить FR-01–FR-11 и SC-01–SC-08 со свидетельствами в `specs/005-vps-migration/acceptance.md` в матрице «подмены / сервер / VK / не наблюдалось». Меры p95, стоимости и доли пустой выдачи (next-stages §3 п. 7) записать по фактическим данным, без подстановки нулей. Поднять TTL A-записи `miniapp` (например, до 3600). Отдельно зафиксировать решение владельца о готовности.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup):** T001–T005 выполнены. T006 выполняется до T007.
- **Phase 2 (Foundational):** T007 → (T008 → T009) параллельно с T010 → T011 → T012 → T013 → T014 → T015. T016 после T012 и параллельно с T014/T015. T017 после T015 и push. T018 ждёт T009 (DNS разошёлся) и T013. T019 после T017. T020 после T015, T017 и T019. T021 и T022 после T020. T015 и T016 блокируют всё последующее: без реестра образов и доступа к VK/Yandex переключение не начинается.
- **Phase 3 (US1, вход):** после T018 и T020. T023 → (T024, T025 параллельно).
- **Phase 4 (US2):** после Checkpoint Phase 3 и T021–T022. Строго последовательно T026 → T032. T030 — точка невозврата: до пустого `diff` ПК остаётся рабочим контуром. T033 → T034 → T035. T036 после T031.
- **Phase 5 (US1, живая матрица):** после T031 (T037) и T035–T036 (T038–T040).
- **Phase 6 (US3):** T041–T045 — в любой момент, параллельно с Phase 2–5. T046–T047 после T031 и T045. T048 после T046. T049 после T035 и T047. T050 после T032.
- **Phase 7:** T051 и T054 начинаются после T049. T052 и T053 — в любой момент после плана. T055 последней.

### User Story Dependencies

- **US1 часть 1 (Phase 3):** зависит только от Foundational. Независимо проверяется до переноса данных.
- **US2 (Phase 4):** зависит от Foundational и от успешного входа US1 часть 1 (иначе переключение не начинается).
- **US1 часть 2 (Phase 5):** зависит от US2: живые ответы требуют перенесённых данных и рабочего bot.
- **US3 (Phase 6):** репозиторные задачи независимы. Серверные задачи зависят от того, что VPS — рабочий контур (T031).

### Parallel Opportunities

- T008 (зона DNS) идёт параллельно с T010–T015 (настройка хоста): смена NS расходится до 48 часов.
- T016 параллельно с T014/T015.
- T024 и T025 параллельно после T023.
- T041, T042, T043 — разные файлы, выполняются параллельно и одновременно с ожиданием DNS или окна переключения.
- T051, T052, T053 параллельны друг другу и наблюдению T054.

---

## Parallel Example: User Story 3

```text
# Репозиторные задачи — во время ожидания DNS (T009):
Task: "T041 deploy/host-alert.sh по contracts/host-alerts.md"
Task: "T042 deploy/cert-check.sh с CERT_WARN_DAYS=14"
Task: "T043 юниты deploy/systemd/ecoprofi-cert-check.{service,timer} и ecoprofi-alert-backup.service"
# затем последовательно: T044 (зависит от T041) → T045 (проверка, коммит)
```

## Parallel Example: User Story 1, часть 1

```text
# После T023 (шлюз отвечает 200):
Task: "T024 certbot reconfigure --webroot и renew --dry-run"
Task: "T025 Test-NetConnection 22/80/443 открыты, 5432/8000/8080 закрыты"
```

---

## Implementation Strategy

### MVP First (US1 часть 1)

1. Phase 1 → Phase 2: сервер, DNS, сертификат, сборка.
2. Phase 3: публичный вход с VPS подтверждён из сетей операторов.
3. **STOP and VALIDATE:** если вход из какой-либо сети не работает, разобрать причину до переключения данных. ПК продолжает работать. Это главный риск функции: недоступность Cloudflare в регионе и стала её основанием.

### Incremental Delivery

1. MVP (вход) → Phase 4: перенос данных в одно окно, единственный контур — VPS.
2. Phase 5: живая матрица 004 на сервере. Mini App работает для участников.
3. Phase 6: надёжность — копии, оповещения, перезагрузка, возврат.
4. Phase 7: семь суток наблюдения и решение владельца.

---

## Notes

- Каждая задача [Ч] завершается записью в acceptance.md. Ненаблюдавшееся остаётся открытым.
- Задачи [Р] не меняют код приложения, схему БД и `compose.yaml` (допущение 4).
- Нумерация изменена 2026-11-24 при перестройке по plan.md. Соответствие прежним номерам: T001→T006, T002–T006→T001–T005, T008–T014→T010–T016, T015→T008/T009, T016–T021→T017–T022, T022–T028→T026–T032, T029→T023, T030→T025/T037, T031→T024, T032→T037, T037–T039→T038–T040, T040→T046, T041–T043→T048–T050, T044→T054/T055. Ссылки T028, T042, T055, T058–T060 в историях относятся к функции 004.

---

## Фактический деплой (2026-11-25)

Все задачи Phase 1–2, T041–T045 и Phase 3 (US1 часть 1) выполнены на VPS 79.174.78.44.

**Способ деплоя:** единый скрипт `deploy/vps-deploy.py` — build локально → docker save → SFTP → docker load → SSH (roles + migrate + up + certbot).

**Выполненные задачи:**

| Задача | Статус | Примечание |
| --- | --- | --- |
| T001–T005 | ✅ | compose.vps.yaml, nginx.conf, backup.sh, table-counts.sql, quickstart |
| T041–T045 | ✅ | host-alert.sh, cert-check.sh, systemd юниты |
| T007 | ✅ | VPS 2 vCPU / 2 ГБ + 2 ГБ swap / 20 ГБ, Ubuntu 24.04, IP 79.174.78.44 |
| T009 | ✅ | DNS на REG.RU, A-запись miniapp → 79.174.78.44 |
| T012 | ✅ | swap 2G, fail2ban, unattended-upgrades |
| T014 | ✅ | Docker CE из репозитория |
| T017 | ✅ | git clone по SSH в /opt/ecoprofi |
| T019 | ✅ | .env scp, chmod 600 |
| T022 | ✅ | БД healthy, роли через db/init/01_roles.sql |
| Migrations | ✅ | 001_pilot.sql, 002_documents.sql, 003_autonomy.sql, 004_miniapp.sql |
| T018/T024 | ✅ | SSL certbot, renew --dry-run успешен |
| T031 | ✅ | bot, db, indexer-daemon, vk-adapter, miniapp-gateway — все Up |
| T036 | ⏳ | требуется проверка доставки оповещений оператору |

**Сервисы (2026-11-25 08:45 MSK):**

| Сервис | Статус | Порт |
| --- | --- | --- |
| bot | ✅ Up | 8000 (internal) |
| db (pgvector) | ✅ healthy | 5432 (internal) |
| indexer-daemon | ✅ Up | internal |
| vk-adapter | ✅ Up | internal |
| miniapp-gateway | ✅ Up | 80, 443, 8080 |
| HTTPS | ✅ 200 OK | miniapp.bot-alimony.ru |

**Невыполненные задачи:** T006 (сайзинг ПК), T008 (DNSSEC), T010–T011 (пользователь deploy, hardening SSH), T013 (ufw), T015 (daemon.json — DNS исправлен, но сеть из контейнеров не работает), T016 (проверка исхода), T020 (compose config -t), T021 (pytest), T023 (вход из сетей), T025 (внешняя поверхность), T026–T035 (перенос данных — не выполнялся, БД на VPS пустая), T037–T040 (живая матрица), T046–T055 (копии, оповещения, наблюдение, приёмка).

**Переход:** после выполнения T006 и T008 → T009 уже выполнен → Phase 4 (перенос данных).
