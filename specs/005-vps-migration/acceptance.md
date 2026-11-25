# Матрица приёмки 005

**Начата:** 2026-11-24. **Последнее обновление:** 2026-11-25. **Статус:** деплой выполнен, все сервисы работают. Перенос данных (Phase 4) не выполнялся — БД на VPS пустая.

## Площадка

| Параметр | Значение |
| --- | --- |
| Провайдер / ЦОД | VPS (OpenStack, Россия) |
| Тариф (vCPU / RAM / диск) | 2 vCPU AMD EPYC-Milan / 2 ГБ + 2 ГБ swap / 20 ГБ NVMe |
| IPv4 | 79.174.78.44 |
| ОС | Ubuntu Server 24.04 LTS |
| Фактическое потребление ПК (T006) | Не снято |
| Путь к образам (Docker Hub / зеркало) (T015) | Локальная сборка → docker save → SFTP → docker load |
| Время переключения контура (T032) | Не выполнено |
| Дата деплоя | 2026-11-25 08:30 MSK |
| Скрипт деплоя | deploy/vps-deploy.py |

## Критерии успеха

| Критерий | Задачи | Статус | Свидетельство |
| --- | --- | --- | --- |
| SC-01 вход из сетей операторов | T023 | Не выполнено | HTTPS 200 с VPS, но вход из сетей не проверен |
| SC-02 количества и контрольные вопросы | T030, T039 | Не выполнено | БД на VPS пустая, перенос данных не выполнен |
| SC-03 живая матрица 004 | T038–T040 | Не выполнено | Зависит от SC-02 |
| SC-04 перезагрузка | T049 | Не выполнено | Зависит от T031 |
| SC-05 восстановление копии | T048 | Не выполнено | Зависит от T046 |
| SC-06 семь суток | T054 | Не выполнено | Зависит от T035 |
| SC-07 сертификат | T024 | ✅ Выполнено | certbot renew --dry-run успешен, истекает 2027-02-23 |
| SC-08 порты и секреты | T025, T037 | Частично | Порты 80/443 открыты, 5432/8000 не проверены извне |

## Вход из сетей (T023)

| Сеть / оператор | Устройство | Дата | Код ответа | Время загрузки | Примечание |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

## Живая матрица 004 на сервере (T038–T040)

Заполняется по столбцам «VK» [acceptance 004](../004-vk-mini-app/acceptance.md): FR-01–FR-14 и SC-01–SC-08. Нужно указывать роль, устройство, клиент VK и исход.

## Наблюдение 7 суток (T054)

| Дата | Проход 07:00 | Проход 19:00 | Автоопросов | Копия ночью | Оповещения | Вмешательства |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

## Возврат на ПК (T050)

Шаги возврата и дата удаления тома ПК — решение владельца: —

## Решение владельца о готовности

—

## Фактический деплой (2026-11-25)

**Скрипт:** `deploy/vps-deploy.py`

**Шаги:**
1. Локальная сборка: `docker build --target app -t ecoprofi-app:local .`
2. Локальная сборка: `docker build --target indexer -t ecoprofi-indexer:local .` (на `python:3.12-slim`)
3. Локальная сборка: `docker build --target miniapp-gateway -t ecoprofi-miniapp-gateway:local .`
4. `docker save -o images.tar ecoprofi-app:local ecoprofi-indexer:local ecoprofi-miniapp-gateway:local`
5. SFTP upload → `docker load < images.tar`
6. `systemctl stop nginx && systemctl disable nginx` (освободить порты 80/443)
7. `docker compose up -d --force-recreate`
8. Создание ролей БД: `db/init/01_roles.sql`
9. Миграции: `docker compose run --rm bot python -m ecoprofi.bot.migrate`
10. Certbot: `certbot --nginx -d miniapp.bot-alimony.ru`
11. Перезапуск сервисов

**Результат:**

| Сервис | Статус | Порт |
| --- | --- | --- |
| bot | ✅ Up | 8000 (internal) |
| db (pgvector) | ✅ healthy | 5432 (internal) |
| indexer-daemon | ✅ Up | internal |
| vk-adapter | ✅ Up | internal |
| miniapp-gateway | ✅ Up | 80, 443, 8080 |
| HTTPS | ✅ 200 OK | miniapp.bot-alimony.ru |
| SSL | ✅ valid | Истекает 2027-02-23 |

**Проблемы и решения:**
- Сеть из Docker-контейнеров к PyPI/Debian не работает (OpenStack internal DNS) → решено локальной сборкой
- tesseract недоступен в Alpine → indexer на `python:3.12-slim`
- pgvector не запускает `.sh` из `initdb.d` → `db/init/01_roles.sql`
- Конфликт портов 80/443 с системным nginx → системный nginx остановлен
