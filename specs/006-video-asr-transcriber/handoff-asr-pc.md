# Инструкция агенту на ПК с распознавателем (006)

Для кого: агент Claude Code или оператор на ПК владельца с видеокартой NVIDIA, где будет работать `asr-worker`. С этого ПК также выполняется деплой на VPS: `deploy/vps-deploy.py` собирает образы локально в Docker и переносит их на сервер.

Ветка: `006-video-asr-transcriber`. Задачи и статус: [tasks.md](tasks.md). Протокол: [acceptance.md](acceptance.md). Сценарии: [quickstart.md](quickstart.md).

## Что уже сделано на ПК разработки (без Docker, БД и GPU)

- **Код сервера:** `ecoprofi/asr/` — `gateway.py`, `jobs.py`, `window.py`, `audio.py`, `publish.py`, `workers.py`, `__main__.py`.
- **Код распознавателя:** `ecoprofi/asr/worker/`.
- **Миграция:** `migrations/005_asr.sql`.
- **Связь с индексатором:**
  - `ecoprofi/indexer/publish.py` — постановка в очередь, защита распознанной версии, отмена задания при субтитрах и удалении;
  - `ecoprofi/indexer/vk_sources.py` — поля `duration`/`date` и `media_streams`.
- **Выдача:** подпись в `ecoprofi/bot/cards.py` и `mini-app/src/components/MaterialCard.tsx`.
- **Инфраструктура:**
  - образы и compose: `Dockerfile` (ffmpeg), `Dockerfile.asr`, `compose.asr.yaml`, сервис `asr-gateway` в `compose.yaml`;
  - шлюз: `deploy/asr-location.inc`, `deploy/vps/miniapp-nginx.conf`, `compose.vps.yaml`;
  - деплой: `deploy/vps-deploy.py` с флагом `--asr-pause`.
- **Тесты:**
  - `tests/asr/` — 31 проверка без БД пройдена, 30 проверок с БД написаны, но **ни разу не запускались**;
  - `tests/integration/test_asr_search.py` — **не запускался**.

Отклонения от текста задач (сделаны сознательно, уже отражены в data-model.md):

- **Имена столбцов.** `asr_schedule.window` называется `window_length`, а `asr_run.only` — `only_refs`: исходные имена — зарезервированные слова SQL. Добавлены столбцы `asr_job.audio_sha256` и `asr_job.deadline_at`.
- **Место постановки в очередь.** Задание ставит и отменяет `ecoprofi/indexer/publish.py` (`publish_version`, `unpublish_deleted`), а не `update.py`. Это та же транзакция, в которой публикуется карточка или субтитры.
- **Фильтр «галлюцинаций».** Отрезок отбрасывается при `compression_ratio > 2.4` или при `no_speech_prob > 0.6` вместе с `avg_logprob < -1.0`. Это стандартное правило Whisper.
- **Сбой публикации.** Задание остаётся `transcribed` с `retry_at` через 15 минут, а не `retry`. Так звук не распознаётся повторно.
- **Образ распознавателя.** `Dockerfile.asr` использует Python 3.10 из Ubuntu 22.04 и ставит только `faster-whisper` и `httpx`, без серверных зависимостей.
- **Раскладка тестов.** Тесты объединены по файлам:

  | Файл | Что проверяет |
  | --- | --- |
  | `test_window.py` | окно |
  | `test_gateway_flow.py` | API, аренда, ключи, секреты |
  | `test_indexer_link.py` | индексатор, защита от карточки, субтитры, удаление |
  | `test_runs_status.py` | миграция, запуски, первичный проход, сбои, сводка, оповещения |
  | `test_audio.py` | подготовка звука |
  | `test_publish_filter.py` | фильтр отрезков |
  | `test_worker_client.py` | клиент на ПК |

## Шаг 1. Подготовка ПК

1. Windows 11, Docker Desktop с бэкендом WSL2, драйвер NVIDIA с поддержкой CUDA 12.
2. Проверить GPU в контейнере:

   ```powershell
   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
   ```

   Модель и объём видеопамяти записать в acceptance.md §0. Если меньше 5 ГБ свободно, в `compose.asr.yaml` поставить `ASR_COMPUTE_TYPE=int8_float16`.
3. Клонировать или обновить репозиторий:

   ```powershell
   git fetch
   git checkout 006-video-asr-transcriber
   git pull
   ```

4. Если на этом ПК поднят стенд из 005 (`docker compose ls` показывает `ecoprofi`), остановить его: `docker compose down`. Тома не удалять. Это требование 005 FR-07: рабочий контур только на VPS.

## Шаг 2. Автотесты в Docker (до любого деплоя)

```powershell
docker compose --profile test run --rm --build tests pytest tests/asr tests/integration/test_asr_search.py -q
docker compose --profile test run --rm tests
```

- Тесты с БД писались без возможности запуска. Падения ожидаемы: чинить **код** там, где тест верно описывает поведение по contracts/asr-api.md и data-model.md, и тест — там, где ошибся сам тест.
- Места, которые стоит проверить первыми:
  - `jobs.py`: таблица `ALLOWED` и `_defer_or_retry` (счёт попыток и `failed_runs`);
  - `gateway.py`: эндпоинт `audio` с Range и зависимость `db` при `StreamingResponse`;
  - `tests/asr/conftest.py`: фикстура `gateway` подменяет `audio.extract` и использует `FakeRunner`;
  - `tests/integration/test_asr_search.py`: материал `card_only_public` должен получить `asr_job` при загрузке стенда.
- Две проверки падают и без изменений 006 при запуске pytest прямо на Windows: `tests/test_access.py::test_login_saves_file_readable_only_by_owner` (chmod) и `tests/test_outcomes.py::test_lead_text_omits_listing_when_cards_shown`. В контейнере их результат нужно сверить с веткой `master`. Если они падают и там, это не регрессия 006.
- Результат записать в acceptance.md, раздел «Автотесты». В tasks.md отметить T066.

## Шаг 3. Деплой на VPS (T035)

Нужны пароль VPS от владельца (не записывать в файлы и протокол) и `pip install paramiko`.

```powershell
$env:VPS_PASSWORD = '<от владельца>'
python deploy/vps-deploy.py --asr-pause
Remove-Item Env:VPS_PASSWORD
```

Скрипт выполняет по порядку:

1. Собирает образы `app`, `indexer` (теперь с ffmpeg) и `miniapp-gateway` (новая подпись в Mini App).
2. Загружает образы и `deploy/vps/miniapp-nginx.conf` с `deploy/asr-location.inc`.
3. Применяет миграции, в том числе `005_asr.sql`.
4. **Ставит распознавание на паузу** (`--asr-pause`), иначе первичный проход начнётся до пробы.
5. Пересоздаёт сервисы с профилями `miniapp` и `daemon`; `asr-gateway` входит в профиль `daemon`.
6. Проверяет `nginx -t` в шлюзе.

Проверить после деплоя (по SSH на VPS, `cd /opt/ecoprofi`):

```bash
ASR="docker compose -f compose.yaml -f compose.vps.yaml --profile indexer run --rm indexer python -m ecoprofi.asr"
docker compose -f compose.yaml -f compose.vps.yaml --profile miniapp --profile daemon ps   # asr-gateway Up
$ASR schedule show            # «ПАУЗА», первичный проход: не начат
$ASR jobs --state queued | head
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://miniapp.bot-alimony.ru/asr/v1/session -H 'Authorization: Bearer x'   # 401
ss -tlnp | grep -v 127.0.0.1  # наружу только 22, 80, 443
docker stats --no-stream      # asr-gateway ≤ 300 МБ
```

- **502 вместо 401.** `asr-gateway` не запущен или упал: смотреть `docker compose logs asr-gateway`.
- **404 вместо 401.** `asr-location.inc` не подключён, проверить монтирование в `compose.vps.yaml`.
- **Не прошёл `nginx -t`.** Шлюз не перезагружать, исправить конфиг.

Если deploy-скрипт неприменим (например, на VPS работа идёт через git clone), сделать вручную: `git pull`, `docker compose … build`, `docker compose … run --rm bot python -m ecoprofi.bot.migrate`, `$ASR pause`, `docker compose … --profile miniapp --profile daemon up -d`.

## Шаг 4. Регистрация распознавателя и запуск на ПК (T036)

1. На VPS выпустить ключ: `$ASR worker add owner-pc`. Ключ показывается один раз; в протокол и журналы его не записывать.
2. На ПК создать `secrets\asr_worker_token` в корне репозитория (каталог в `.gitignore`), содержимое — ключ одной строкой. Ограничить права текущим пользователем:

   ```powershell
   icacls secrets\asr_worker_token /inheritance:r /grant:r "$env:USERNAME:R"
   ```

3. Собрать и запустить распознаватель:

   ```powershell
   docker compose -f compose.asr.yaml up -d --build
   docker compose -f compose.asr.yaml logs -f asr-worker
   ```

   Первый запуск скачивает модель large-v3 (~3 ГБ) в том `asr-models`; это делается один раз.
4. Ожидается запись «на связи, окно закрыто» (пауза). На VPS `$ASR worker list` показывает связь не старше 5 минут и GPU.

## Шаг 5. Проба на двух записях (T037, SC-01)

1. `$ASR jobs --state queued` — выбрать одну открытую запись и одну из платной ленты клуба.
2. `$ASR probe <owner>_<item>` по каждой. Записать поля медиа, отдельную дорожку, время подготовки, размер; во время пробы — `docker stats` на VPS.
3. Распознать только эти две записи, пауза при этом сохраняется:

   ```bash
   $ASR run-now --only <owner1>_<item1>,<owner2>_<item2>
   ```

   В течение 5 минут распознаватель берёт их; запуск закрывается с `queue_empty`.
4. `$ASR status` и `$ASR jobs` — длительность, время, исходы. Сверить по три отрезка с плеером: расхождение не больше ±5 с. Таймкоды отрезков — в `asr_transcript.segments` (`psql` на VPS, только чтение).
5. Тома `asr-tmp` пусты:
   - на ПК: `docker compose -f compose.asr.yaml exec asr-worker ls -A /tmp/asr`;
   - на VPS: `docker compose … exec asr-gateway ls -A /tmp/asr`.
6. **Если `video.get` не отдаёт медиа для клубной записи** (`audio_unavailable`), это открытый вопрос владельцу. Запасной вариант B только его решением, скрытых обходов не делать.

## Шаг 6. Поиск (T045, SC-02)

После публикации контрольной записи задать в Mini App три вопроса под ролью подписчика, затем проверить отказ неподписчику. Таблицу заполнить в acceptance.md §4.

## Шаг 7. Первичный проход и график (T051, T052)

1. `$ASR resume` — начинается первичный проход по всей начальной очереди. Записать время начала: SC-03 требует не позже 15 минут после развёртывания.
2. Посреди записи выключить ПК, через 30 минут включить. Ожидается: запуск закрыт `worker_lost`, задание вернулось, проход продолжился, частичной версии нет.
3. По завершении: `$ASR status` («Первичный проход: завершён») и `$ASR schedule show` (вс 01:00 МСК).
4. Неделя наблюдения по quickstart §5.

## Шаг 8. Остальные проверки

| Задача | Что сделать |
| --- | --- |
| T057 | quickstart §6 — повторы и субтитры |
| T062 | сводка, скорость ПК (SC-09) |
| T067 | quickstart §7 — `grep` журналов на VPS и `Select-String` на ПК |
| T068 | ресурсы VPS при подготовке звука |
| T069 | итоговая матрица SC в acceptance.md |

После каждого шага отмечать задачу `[X]` в tasks.md и коммитить протокол без секретов.

## Если что-то пошло не так

- **Остановить распознавание:** `$ASR pause`. Сервер, Mini App и индексатор от распознавателя не зависят.
- **Откатить сервис:** `docker compose … stop asr-gateway`; шлюз отдаёт 502 только на `/asr/`.
- **Отозвать ключ при утечке:** `$ASR worker revoke owner-pc`, затем `worker add` с новым именем.
- **Остановить распознаватель на ПК:** `docker compose -f compose.asr.yaml down` (тома модели сохраняются).
