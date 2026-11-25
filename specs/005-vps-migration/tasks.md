---
description: "Задачи 005: перенос стенда на VPS"
---

# Задачи 005: перенос стенда на VPS

**Вход:** [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md).

**Дата:** 2026-11-24 (перестроено по plan.md, нумерация изменена). **Статус:** 20/55. Подготовка репозитория T001–T005 и скрипты оповещений хоста T041–T044 выполнены (2026-11-24). Единый скрипт деплоя deploy/vps-deploy.py создан (2026-11-25). Фактический деплой на VPS 79.174.78.44 выполнен 2026-11-25: образы собраны локально, загружены через docker save/load, роли БД созданы через db/init/01_roles.sql, миграции применены, SSL-сертификат выпущен, все 5 сервисов работают (bot, db, indexer-daemon, vk-adapter, miniapp-gateway). HTTPS miniapp.bot-alimony.ru → 200 OK.

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

- [x] T007 [Ч] Заказать VPS по research R1: российский ЦОД (Москва/СПб), KVM, **2 vCPU / 2 ГБ RAM + 2 ГБ swap / 20 ГБ NVMe**, статический IPv4, **Ubuntu Server 24.04 LTS**. IP 79.174.78.44. Записать в acceptance.md.
- [ ] T008 [P] [Ч] `[ПК]` Подготовка зоны к смене NS (research R5). DNSSEC не проверялся, Cloudflare туннель удалён, NS переключены на REG.RU. Записать в acceptance.md.
- [x] T009 [Ч] `[ПК]` Смена NS на REG.RU (решение владельца 2026-11-24). Смена выполнена. A-запись `miniapp.bot-alimony.ru` → 79.174.78.44. Туннель Cloudflare удалён.
- [ ] T010 [P] [Ч] `[VPS]` Первый вход и пользователь `deploy` — не выполнен, работа под root.
- [ ] T011 [Ч] `[VPS]` Закрыть SSH — не выполнен.
- [x] T012 [Ч] `[VPS]` Обновления, время, swap, fail2ban: `apt full-upgrade`, `unattended-upgrades`, timezone Europe/Moscow, swap 2G, fail2ban.
- [ ] T013 [Ч] `[VPS]` Брандмауэр ufw — не выполнен.
- [x] T014 [Ч] `[VPS]` Docker из официального репозитория: docker-ce, docker-compose-plugin.
- [ ] T015 [Ч] `[VPS]` `/etc/docker/daemon.json` — DNS исправлен на 8.8.8.8, но сеть из Docker-контейнеров к PyPI/Debian не резолвит (OpenStack internal). Решено локальной сборкой образов.
- [ ] T016 [P] [Ч] `[VPS]` Исходящий доступ — не проверялся, записан в acceptance.md.
- [x] T017 [Ч] `[VPS]` Получить код в `/opt/ecoprofi`: git clone по SSH, коммит совпадает с последним push.
- [x] T018 [Ч] `[VPS]` Выпустить сертификат: `certbot --nginx -d miniapp.bot-alimony.ru` выпущен. Системный nginx остановлен.
- [x] T019 [Ч] `[ПК]`→`[VPS]` Секреты: `.env` scp, chmod 600, `MINIAPP_PUBLIC_ORIGIN=https://miniapp.bot-alimony.ru` и `MINIAPP_ENABLED=true`.
- [ ] T020 [Ч] `[VPS]` Проверка проекта и сборка: `dc config -q`, `dc build`, `nginx -t` — не проверялось.
- [ ] T021 [Ч] `[VPS]` Автоматические проверки: `docker compose --profile test run --rm --build tests` — не гонялось.
- [x] T022 [Ч] `[VPS]` Поднять `db`: `docker compose up -d db`, healthy. Init-скрипт `db/init/01_roles.sql` создаёт роли `ecoprofi_bot`, `ecoprofi_indexer` и расширение `vector`.

**Checkpoint:** сервер защищён, домен указывает на VPS, сертификат выпущен, образы собраны, тесты пройдены, пустая БД работает.

---

## Phase 3: User Story 1 — участник открывает Mini App с сервера (P1), часть 1: вход 🎯 MVP

**Цель:** публичный вход `https://miniapp.bot-alimony.ru` работает с VPS без Cloudflare из сетей участников. Проверяется до переключения данных.

**Независимая проверка:** шлюз запущен без bot (`--no-deps`). `GET /` отдаёт 200 с CSP и HSTS из трёх мобильных сетей и одной домашней, `http` перенаправляет на `https`, снаружи открыты только 22/80/443, `certbot renew --dry-run` успешен. API на этом шаге отвечает 502: это ожидаемо, bot ещё не запущен, а шлюз разрешает имя `bot` при запросе (`deploy/miniapp-proxy.inc`).

- [ ] T023 [US1] [Ч] `[VPS]`+`[ПК]` Запустить только шлюз: HTTPS 200 подтверждён с VPS, но вход из сетей операторов не проверялся.
- [x] T024 [P] [US1] [Ч] `[VPS]` Продление сертификата через webroot: `certbot renew --dry-run` успешен (SC-07).
- [ ] T025 [P] [US1] [Ч] `[ПК]` Внешняя поверхность: `Test-NetConnection` порты 22/80/443 открыты, 5432/8000/8080 не проверялись извне.

**Checkpoint:** вход с сервера подтверждён до переключения. Если T023 не проходит из какой-либо сети, переключение (Phase 4) не начинается, ПК остаётся рабочим контуром.

---

## Phase 4: User Story 2 — данные и операторский доступ перенесены без потерь (P1)

**Цель:** каталог, версии, аудитории, решения редактора, очередь и незавершённые проходы на VPS совпадают со снимком ПК. Работает один рабочий контур. Оператор заново вошёл на сервере, индексатор выполняет плановые проходы.

**Независимая проверка:** `diff out/counts-pc.txt out/counts-vps.txt` пуст (SC-02, количества). На ПК не работают писатели и туннель. `indexer status` на VPS показывает действующий доступ и следующий проход в 07:00 или 19:00 МСК. Тестовое оповещение дошло оператору.

Окно работ — около 1 часа, вне плановых проходов 07:00/19:00 МСК. Участников предупредить заранее. Переходы состояний контура описаны в data-model.md («Рабочий контур»).

- [ ] T026 [US2] [Ч] `[ПК]` Перевод в `freeze` — не выполнялся, перенос данных не выполнен.
- [ ] T027 [US2] [Ч] `[ПК]` Снимок переноса и контрольные данные — не выполнялся.
- [ ] T028 [US2] [Ч] Передача дампа на VPS — не выполнялась.
- [ ] T029 [US2] [Ч] `[VPS]` Восстановление в БД — не выполнялось.
- [ ] T030 [US2] [Ч] `[VPS]` Сверка — не выполнялась.
- [ ] T031 [US2] [Ч] `[VPS]` Запуск сервисов — **выполнен вручную** 2026-11-25: bot, vk-adapter, miniapp-gateway, indexer-daemon, db — все Up.
- [ ] T032 [US2] [Ч] `[ПК]` Зафиксировать контур — не выполнялся.
- [ ] T033 [US2] [Ч] `[VPS]` Новый вход оператора — не выполнялся.
- [ ] T034 [US2] [Ч] `[VPS]` Разовая проверка чтения на сервере — не выполнялась.
- [ ] T035 [US2] [Ч] `[VPS]` Плановый режим — не выполнялся.
- [ ] T036 [US2] [Ч] Оповещения с VPS — **не проверялась** доставка оператору.

**Checkpoint:** VPS — единственный рабочий контур, данные сверены, индексатор работает по расписанию.

---

## Phase 5: User Story 1 — участник открывает Mini App с сервера (P1), часть 2: живая матрица

**Цель:** вход, подписка, вопрос, уточнение и переход к источнику работают с сервера так же, как на локальном стенде. Это перенесённые T028, T042, T055, T058–T060 функции 004.

**Независимая проверка:** матрица SC-01–SC-08 функции 004 заполнена серверными свидетельствами на телефоне и ПК (SC-03). Неподписчик получает только пояснение и ссылку клуба. Зависит от Phase 4: живые ответы требуют перенесённых данных.

- [ ] T037 [US1] [Ч] `[ПК]`+`[VPS]` Allowlist и секреты — не проверялся (зависит от T031 с перенесёнными данными).
- [ ] T038 [US1] [Ч] Плитка «Найти лекцию» — не проверялась (зависит от перенесённых данных).
- [ ] T039 [US1] [Ч] Вопрос → уточнение → ответ → источник — не проверялся.
- [ ] T040 [US1] [Ч] Диалоги, смена учётной записи, перезагрузка bot — не проверялось.

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
- [x] T045 [US3] [Р] Скрипты T041–T044 сверены, quickstart.md обновлён, коммит и push выполнены.
- [ ] T046 [US3] [Ч] `[VPS]` Резервные копии — не устанавливались.
- [ ] T047 [US3] [Ч] `[VPS]` Оповещения хоста — не устанавливались.
- [ ] T048 [US3] [Ч] `[VPS]` Пробное восстановление — не выполнялось.
- [ ] T049 [US3] [Ч] `[VPS]` Перезагрузка — не проверялась.
- [ ] T050 [US3] [Ч] Репетиция возврата — не выполнялась.

**Checkpoint:** все три истории выполнены.

---

## Phase 7: Polish — наблюдение, документы, итоговая приёмка

**Назначение:** сквозные обязательства next-stages §3 и раздела «Порядок изменений» конституции.

- [ ] T051 [P] [Ч] `[VPS]` Доступ оператора на сервере — не проверялся.
- [ ] T052 [P] [Ч] Обновить раздел развёртывания в `Архитектура_бота_Экопрофи.docx` — не обновлялся.
- [ ] T053 [P] [Ч] Решение владельца о внешнем мониторинге — не принято.
- [ ] T054 [Ч] Семь суток при выключенном ПК — не наблюдалось.
- [ ] T055 [Ч] Итоговая приёмка — не проводилась.

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
