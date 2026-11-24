# Задачи 005: перенос стенда на VPS

**Дата:** 2026-11-24. **Статус:** 5/44. Фаза 0 в репозитории выполнена (T002–T006). `nginx -t` и запуск оверлея проверяются на VPS в T019, потому что на ПК разработки Docker не установлен. Спецификация — [spec.md](spec.md), команды — [quickstart.md](quickstart.md).

**Обозначения.** **[Ч]** — выполняет человек (владелец/оператор) по шагам. **[Р]** — изменение репозитория, выполняет разработчик/Claude до шагов человека. `[ПК]` — команды PowerShell на текущем ПК из папки проекта. `[VPS]` — команды bash на сервере под пользователем `deploy` в `/opt/ecoprofi`. Результат каждой задачи **[Ч]** записывается в `specs/005-vps-migration/acceptance.md`: дата, факт, отклонения. Секреты в протокол не вносятся.

## Фаза 0. Подготовка репозитория [Р]

- [ ] T001 [Ч] `[ПК]` Снять фактическое потребление для сайзинга: `docker stats --no-stream`, `docker system df -v`, размер тома БД `docker compose exec -T db psql -U ecoprofi_owner -d ecoprofi -tAc "SELECT pg_size_pretty(pg_database_size('ecoprofi'));"`. Записать результат и при необходимости скорректировать тариф по правилу из раздела «Сайзинг» spec.md.
- [x] T002 [Р] `compose.vps.yaml` (оверлей): шлюз публикует `80:80` и `443:443` в дополнение к диагностическому `127.0.0.1:8080`, монтирует VPS-конфиг nginx, `/etc/letsencrypt` (ro) и `/var/www/certbot` (ro). Профиль `tunnel` на сервере не используется.
- [x] T003 [Р] `deploy/vps/miniapp-nginx.conf`: порт 80 (`/.well-known/acme-challenge/` из webroot, остальное — 301 на https), порт 443 с сертификатом Let's Encrypt и тем же `miniapp-server.inc` (CSP, HSTS, allowlist), отказ рукопожатия для чужих имён и обращений по IP, 8080 для диагностики. `miniapp-realip.inc` не подключается: доверия `CF-Connecting-IP` нет. Образ шлюза не меняется, конфиг для ПК остаётся прежним.
- [x] T004 [Р] `deploy/backup.sh`: `pg_dump -Fc` → проверка `pg_restore --list` → sha256 → rclone в S3 daily/ и weekly/ (воскресенье) → ротация 7 дней и 4 недели, локально 3 копии. Ненулевой код возврата при ошибке. Unit-файлы `deploy/systemd/ecoprofi-backup.{service,timer}` на 03:30 МСК.
- [x] T005 [Р] `deploy/table-counts.sql`: точные количества строк всех таблиц `public` (через `query_to_xml`) и список `schema_migrations`.
- [x] T006 [Р] `specs/005-vps-migration/quickstart.md` с командами и `acceptance.md` с пустой матрицей. Коммит в `master`. Push выполняется до T016.

## Фаза 1. Заказ и базовая настройка VPS [Ч]

- [ ] T007 [Ч] Заказать VPS: российский ЦОД (Москва/СПб), KVM, **4 vCPU / 8 ГБ RAM / 80 ГБ NVMe** (минимум 2/4/40), статический IPv4, ОС **Ubuntu Server 24.04 LTS**. При заказе добавить свой публичный SSH-ключ. Включить снимки провайдера и создать S3-бакет (10–20 ГБ) с отдельным ключом доступа только к нему. Записать провайдера, тариф, IP (IP — не секрет).
- [ ] T008 [Ч] Первый вход и пользователь:
  ```bash
  ssh root@<IP>
  adduser deploy && usermod -aG sudo deploy
  mkdir -p /home/deploy/.ssh && cp ~/.ssh/authorized_keys /home/deploy/.ssh/ && chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
  ```
  Проверить вход `ssh deploy@<IP>` в **новом** окне, не закрывая старое.
- [ ] T009 [Ч] Закрыть SSH: в `/etc/ssh/sshd_config.d/10-hardening.conf` указать `PermitRootLogin no`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`. Затем `sudo systemctl restart ssh` и повторная проверка входа под deploy в новом окне.
- [ ] T010 [Ч] Обновления, время и swap:
  ```bash
  sudo apt update && sudo apt full-upgrade -y
  sudo apt install -y unattended-upgrades fail2ban ufw git curl ca-certificates rclone
  sudo dpkg-reconfigure -plow unattended-upgrades
  sudo timedatectl set-timezone Europe/Moscow && timedatectl   # NTP synchronized: yes
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  ```
- [ ] T011 [Ч] Брандмауэр: `sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable`. Важно: Docker публикует порты в обход ufw, поэтому в Compose наружу публикуются только 80/443 шлюза (проверка — T030).
- [ ] T012 [Ч] Установить Docker из официального репозитория:
  ```bash
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list
  sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker deploy && sudo systemctl enable docker
  ```
  Перелогиниться и проверить: `docker version`, `docker compose version`.
- [ ] T013 [Ч] Проверить доступность реестров заранее: `docker pull python:3.12-slim node:24.14.1-bookworm-slim nginx:1.29.3-alpine pgvector/pgvector:pg16`. При ошибке настроить зеркало провайдера в `/etc/docker/daemon.json` (`"registry-mirrors": [...]`), выполнить `sudo systemctl restart docker` и повторить. Записать, какой путь сработал.
- [ ] T014 [Ч] Проверить исходящий доступ к внешним сервисам стенда: `curl -sSI https://api.vk.com/method/`, `curl -sSI https://llm.api.cloud.yandex.net`, `curl -sSI https://github.com`. Нужны любые HTTP-ответы, а не тайм-ауты.

## Фаза 2. Домен и сертификат [Ч]

- [ ] T015 [Ч] DNS на REG.RU (решение владельца 2026-11-24). **Начинать сразу после T007:** смена NS расходится до 24–48 часов.
  1. В Cloudflare → DNS → Records выписать **все** записи зоны, кроме `miniapp` (MX, TXT/SPF/DKIM, прочие A/CNAME). Иначе почта и другие сервисы домена пропадут после смены NS.
  2. В REG.RU → домен `bot-alimony.ru` → «DNS-серверы и управление зоной» выбрать «Использовать DNS-серверы REG.RU» (`ns1.reg.ru`, `ns2.reg.ru`).
  3. В зоне REG.RU создать выписанные записи и `A miniapp → <IP VPS>` с TTL 300.
  4. В Cloudflare удалить туннель `ecoprofi-miniapp` (Networks → Tunnels), затем удалить сайт из аккаунта.
  5. Проверить с ПК: `Resolve-DnsName bot-alimony.ru -Type NS -Server 8.8.8.8` → `ns*.reg.ru`; `Resolve-DnsName miniapp.bot-alimony.ru -Server 8.8.8.8` и `-Server 77.88.8.8` → IP VPS. Пока ответы не совпадут, T017 не выполнять.
- [ ] T016 [Ч] `[VPS]` Получить код:
  ```bash
  sudo mkdir -p /opt/ecoprofi && sudo chown deploy:deploy /opt/ecoprofi
  ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""   # публичную часть добавить в GitHub → Settings → Deploy keys (read-only)
  GIT_SSH_COMMAND="ssh -i ~/.ssh/github_deploy" git clone git@github.com:Evcher-arch/ecoprofi_bot.git /opt/ecoprofi
  cd /opt/ecoprofi && git config core.sshCommand "ssh -i ~/.ssh/github_deploy" && git log -1 --oneline
  ```
  Коммит должен совпадать с T006.
- [ ] T017 [Ч] `[VPS]` Выпустить сертификат (до запуска шлюза, порт 80 свободен):
  ```bash
  sudo apt install -y certbot
  sudo certbot certonly --standalone -d miniapp.bot-alimony.ru -m <почта владельца> --agree-tos --no-eff-email
  sudo mkdir -p /var/www/certbot
  ```
  Создать хук перезагрузки шлюза `reload-miniapp.sh` (quickstart, раздел «Сертификат»). Переключение продления на webroot (`certbot reconfigure`) выполняется в T031, когда шлюз уже занимает порт 80.

## Фаза 3. Секреты и пробный запуск без данных [Ч]

- [ ] T018 [Ч] `[ПК]` Передать секреты только по SSH: `scp .env deploy@<IP>:/opt/ecoprofi/.env`. Затем `[VPS]` `chmod 600 .env`. Проверить, что `MINIAPP_PUBLIC_ORIGIN=https://miniapp.bot-alimony.ru` и `MINIAPP_ENABLED=true`. Строки и токены в чат и протокол не копировать. `deploy/secrets/cloudflared-token.txt` на сервер **не** переносить.
- [ ] T019 [Ч] `[VPS]` Собрать образы: `docker compose -f compose.yaml -f compose.vps.yaml --profile miniapp --profile daemon build`. Проверить nginx: `docker compose -f compose.yaml -f compose.vps.yaml --profile miniapp run --rm --no-deps --entrypoint nginx miniapp-gateway -t`.
- [ ] T020 [Ч] `[VPS]` Прогнать автоматические проверки на сервере: `docker compose --profile test run --rm --build tests`. Сравнить число пройденных и пропущенных тестов с последним прогоном на ПК.
- [ ] T021 [Ч] `[VPS]` Поднять только `db`: `docker compose up -d db`. Дождаться healthy (`docker compose ps`). Init-скрипт создаёт роли `ecoprofi_bot` и `ecoprofi_indexer`. Других сервисов пока не запускать.

## Фаза 4. Переключение рабочего контура [Ч]

Окно работ — около 1 часа, лучше вне плановых проходов 07:00/19:00 МСК. Участников предупредить заранее.

- [ ] T022 [Ч] `[ПК]` Убедиться, что индексатор не выполняет проход (`docker compose --profile indexer run --rm indexer python -m ecoprofi.indexer status`). Остановить всех писателей и вход: `docker compose --profile miniapp --profile tunnel --profile daemon stop vk-adapter indexer-daemon bot miniapp-tunnel miniapp-gateway`. `db` оставить работать. **Не выполнять `down -v`.**
- [ ] T023 [Ч] `[ПК]` Снимок и контрольные данные:
  ```powershell
  New-Item -ItemType Directory -Force out | Out-Null
  docker compose exec -T db pg_dump -U ecoprofi_owner -d ecoprofi -Fc -f /tmp/ecoprofi.dump
  docker compose cp db:/tmp/ecoprofi.dump out/ecoprofi-migration.dump
  Get-FileHash out/ecoprofi-migration.dump -Algorithm SHA256
  docker compose cp deploy/table-counts.sql db:/tmp/counts.sql
  docker compose exec -T db sh -c "psql -U ecoprofi_owner -d ecoprofi -tA -f /tmp/counts.sql > /tmp/counts.txt"
  docker compose cp db:/tmp/counts.txt out/counts-pc.txt
  ```
  Файл сверки формируется внутри контейнера, поэтому кодировка вывода PowerShell не портит `diff` на VPS.
  Записать SHA256 и размер. Дамп содержит закрытые данные: хранить только в `out/` (не в Git).
- [ ] T024 [Ч] `[ПК]` Передать: `scp out/ecoprofi-migration.dump out/counts-pc.txt deploy@<IP>:/opt/ecoprofi/out/` (каталог `out` предварительно создать на VPS). `[VPS]` `sha256sum out/ecoprofi-migration.dump` — хэш совпадает с T023.
- [ ] T025 [Ч] `[VPS]` Восстановить:
  ```bash
  docker compose cp out/ecoprofi-migration.dump db:/tmp/ecoprofi.dump
  docker compose exec -T db pg_restore -U ecoprofi_owner -d ecoprofi --exit-on-error /tmp/ecoprofi.dump
  docker compose run --rm bot python -m ecoprofi.bot.migrate      # ожидается: новых миграций нет
  ```
- [ ] T026 [Ч] `[VPS]` Сверка: `docker compose exec -T db psql -U ecoprofi_owner -d ecoprofi -tA < deploy/table-counts.sql > out/counts-vps.txt && diff out/counts-pc.txt out/counts-vps.txt`. Пустой `diff` — SC-02 (количества). При расхождении не продолжать: разобрать причину, ПК остаётся рабочим контуром (вернуть сервисы ПК командой `up -d`).
- [ ] T027 [Ч] `[VPS]` Запустить сервисы: `docker compose -f compose.yaml -f compose.vps.yaml --profile miniapp up -d bot vk-adapter miniapp-gateway`. Затем `docker compose ps` — все Up. `indexer-daemon` пока не запускать (T033).
- [ ] T028 [Ч] `[ПК]` Зафиксировать контур: на ПК `docker compose stop db`. Записать дату и время переключения. Том `db-data` на ПК сохраняется не менее 14 дней (FR-07). Снять автозапуск сервисов ПК: `docker update --restart=no $(docker ps -aq --filter label=com.docker.compose.project=ecoprofi)`.

## Фаза 5. Проверка входа и безопасности [Ч]

- [ ] T029 [Ч] Внешний вход с ПК и телефона: `curl.exe -sSI https://miniapp.bot-alimony.ru/` → 200, CSP с `frame-ancestors` VK, HSTS. `curl.exe -sSI http://miniapp.bot-alimony.ru/` → 301 на https. Открыть адрес с телефона через сети **трёх разных мобильных операторов** и одного домашнего провайдера (SC-01). Записать оператора, результат и время загрузки.
- [ ] T030 [Ч] Allowlist и порты: `POST /api/miniapp/v1/session` без подписи → 4xx; `/api/search`, `/api/delivered`, `/api/alerts/pending`, `/docs`, `/openapi.json` → 404. Снаружи (с ПК) `Test-NetConnection <IP> -Port 5432`, `-Port 8000`, `-Port 8080` → False. Порты 22/80/443 → True (SC-08).
- [ ] T031 [Ч] `[VPS]` Сертификат: `sudo certbot reconfigure --cert-name miniapp.bot-alimony.ru --webroot -w /var/www/certbot`, затем `sudo certbot renew --dry-run` — успешно (SC-07). Дополнительно — внешний тест SSL (например, SSL Labs) с оценкой не ниже A.
- [ ] T032 [Ч] `[VPS]` Секреты в логах: `docker compose logs --no-color | grep -Ei "sign=|token|secret|bearer|password"` — совпадений с реальными значениями нет. Записать проверенные шаблоны.

## Фаза 6. Индексатор и операторский доступ (OPS-01, NET-01) [Ч]

- [ ] T033 [Ч] `[VPS]` Новый вход оператора на сервере: `docker compose --profile indexer run --rm indexer python -m ecoprofi.indexer access login`. Затем `... indexer status`. Том `indexer-secrets` с ПК не копировать.
- [ ] T034 [Ч] `[VPS]` Разовая проверка чтения на сервере: расписание, платная лента, документ, дорожка (OPS-01). Использовать команды индексатора из quickstart 003. Записать исходы. Отказ доступа, пустой ответ, сетевой сбой и ошибка разбора различаются (NET-01).
- [ ] T035 [Ч] `[VPS]` Запустить плановый режим: `docker compose --profile daemon up -d indexer-daemon`. Убедиться, что следующий проход назначен на 07:00 или 19:00 МСК и активный проход один.
- [ ] T036 [Ч] Оповещения: убедиться, что тестовое или реальное оповещение дошло оператору (`ALERT_VK_USER_ID`) с VPS.

## Фаза 7. Живая приёмка Mini App (перенесено из 004) [Ч]

- [ ] T037 [Ч] Плитка «Найти лекцию» в сообществе «Экопрофи» на телефоне и ПК: вход без ручного ввода ID и пароля, роли подписчик / неподписчик / исключение (бывш. T028 004).
- [ ] T038 [Ч] Вопрос → уточнение → ответ → источник для эпизода, презентации, письма и записи без текста. Сверка интервала, страницы и реквизитов с оригиналом. Отсутствие сообщений у администрации (бывш. T042 004). Четыре контрольных вопроса — те же источники, что на ПК (SC-02).
- [ ] T039 [Ч] Две учётные записи × два диалога, перезагрузка вкладки, смена учётной записи. Остановка и запуск bot при открытом чате (`docker compose stop bot` / `start bot`), ошибка, а не «не найдено» (бывш. T055, T059 004). Заполнить матрицу SC-01–SC-08 функции 004 серверными свидетельствами (SC-03).

## Фаза 8. Надёжность и резервное копирование [Ч]

- [ ] T040 [Ч] `[VPS]` Резервные копии (quickstart, раздел «Резервная копия»): создать `/etc/ecoprofi-backup.env` с `RCLONE_REMOTE`, настроить `rclone config` на S3-бакет (ключ только в конфиге rclone, права 600). Установить unit-файлы T004 (`sudo cp deploy/systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now ecoprofi-backup.timer`). Выполнить разово `sudo systemctl start ecoprofi-backup.service` и проверить файл в бакете.
- [ ] T041 [Ч] `[VPS]` Пробное восстановление (SC-05): скачать последнюю копию из S3, поднять **изолированную** БД (`docker run --rm -d --name restore-check -e POSTGRES_PASSWORD=<временный> pgvector/pgvector:pg16`, без сети `internal`, без bot/indexer), восстановить, прогнать `deploy/table-counts.sql` и сравнить. Записать длительность и давность копии. Удалить контейнер.
- [ ] T042 [Ч] `[VPS]` Перезагрузка (SC-04): `sudo reboot`, через 3 минуты — `docker compose -f compose.yaml -f compose.vps.yaml --profile miniapp --profile daemon ps` (все Up/healthy), `curl -sSI https://miniapp.bot-alimony.ru/` → 200, проход индексатора по расписанию не сбит.
- [ ] T043 [Ч] Репетиция возврата (без реального переключения): описать в acceptance.md шаги возврата на ПК — остановить писателей на VPS → свежий дамп VPS → восстановление на ПК → A-запись на прежний вход → запуск ПК. Проверить, что том ПК цел (`docker volume inspect ecoprofi_db-data`). Записать решение владельца, когда удалить том ПК после 14 дней.

## Фаза 9. Наблюдение и приёмка [Ч]

- [ ] T044 [Ч] Семь суток при выключенном ПК (SC-06): ежедневно записывать исходы проходов 07:00/19:00, число автоматических опросов, оповещения, наличие свежей ночной копии и вмешательства. Затем сверить FR-01–FR-11 и SC-01–SC-08 со свидетельствами в `acceptance.md` и отдельно зафиксировать решение владельца о готовности. Меры p95, стоимости и доли пустой выдачи (next-stages §3 п. 7) записать по фактическим данным, без подстановки нулей.

## Зависимости

Фаза 0 → 1 → 2 → 3 → 4 → (5, 6 параллельно) → 7 → 8 → 9. T015 (смена NS) начинается сразу после T007 и идёт параллельно с T008–T014; T017 ждёт её результата. T013 и T014 блокируют всё последующее: без реестра образов и доступа к VK/Yandex переключение не начинается. T026 — точка невозврата: до пустого `diff` ПК остаётся рабочим контуром. T001 выполняется до T007.
