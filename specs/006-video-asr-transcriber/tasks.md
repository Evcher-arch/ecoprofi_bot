---
description: "Задачи 006: распознавание записей без субтитров"
---

# Задачи 006: распознавание записей без субтитров

**Вход:** [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/asr-api.md](contracts/asr-api.md), [contracts/operator-commands.md](contracts/operator-commands.md), [quickstart.md](quickstart.md).

**Дата:** 2026-11-27. **Статус:** 56/69 (2026-11-28). Код, миграция, конфигурация и тесты написаны; тесты без БД пройдены, тесты с БД, деплой и пробы — на ПК с распознавателем по [handoff-asr-pc.md](handoff-asr-pc.md).

**Тесты.** Запрошены планом (`tests/asr/`) и quickstart §8. Пишутся до реализации своей истории и должны сначала падать. Запуск: `docker compose --profile test run --rm tests pytest tests/asr`.

**Обозначения.**

- `[P]` — можно выполнять параллельно с соседними задачами: другие файлы, нет зависимости от незавершённых задач.
- `[US1]`–`[US5]` — история из spec.md.
- `[Ч]` — выполняет человек (владелец или оператор) по шагам quickstart. Остальные задачи — изменение репозитория.
- `$ASR` — `docker compose -f compose.yaml -f compose.vps.yaml --profile indexer run --rm indexer python -m ecoprofi.asr`.
- Результат каждой задачи `[Ч]` записывается в `specs/006-video-asr-transcriber/acceptance.md` с полями принципа V: дата, среда, способ авторизации, материал, наблюдаемый результат, открытый вопрос. Секреты, ключи и временные адреса VK в протокол не вносятся.

**Константы** (из research R4–R6 и contracts/asr-api.md; держать в одном месте — `ecoprofi/asr/__init__.py`):

| Имя | Значение |
| --- | --- |
| `PROCESSOR_VERSION` | `asr-1/large-v3` |
| `LEASE_TTL` | 20 минут |
| `HEARTBEAT_EVERY` | 2 минуты |
| `SESSION_EVERY` | 5 минут |
| `DEADLINE` | 1,5 × длительность звука + 15 минут |
| `ATTEMPTS_PER_RUN` | 3 |
| `PERMANENT_AFTER_RUNS` | 3 (как в индексаторе) |
| `MIN_SPEECH_CHARS` | 200 символов после фильтра |
| `AUDIO_RECHECK` | 7 суток для `audio_unavailable` |
| `CLIENT_BACKOFF` | экспоненциально от 5 секунд до 5 минут |

---

## Phase 1: Setup — пакет, образы, конфигурация

**Назначение:** каркас пакета `ecoprofi/asr`, зависимости и инфраструктурные файлы без логики.

- [X] T001 Создать пакет `ecoprofi/asr/` с файлами `__init__.py` (константы из таблицы выше), `__main__.py`, `gateway.py`, `jobs.py`, `window.py`, `audio.py`, `publish.py`, `workers.py` и подпакет `ecoprofi/asr/worker/` с `__init__.py`, `__main__.py`, `client.py`, `transcribe.py`. В модулях — только docstring с назначением из plan.md «Source Code». `ecoprofi/asr/worker/` не импортирует ничего из `ecoprofi.*`, кроме самого `ecoprofi.asr.worker`.
- [X] T002 [P] Добавить в `pyproject.toml` группу `asr-worker = ["faster-whisper>=1.0,<2", "httpx>=0.27,<1"]` в `[project.optional-dependencies]`. Серверные зависимости не меняются: FastAPI, uvicorn, psycopg и httpx уже есть в основном списке.
- [X] T003 [P] В `Dockerfile`, стадия `indexer-base`, добавить `ffmpeg` в существующую строку `apt-get install -y --no-install-recommends` (рядом с `tesseract-ocr`). Другие стадии не меняются.
- [X] T004 [P] Создать `Dockerfile.asr` для ПК: `FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04`, Python 3.12 (deadsnakes или `python3.12` из apt), `pip install ".[asr-worker]"` по `pyproject.toml`, копировать только `ecoprofi/__init__.py` и `ecoprofi/asr/__init__.py` + `ecoprofi/asr/worker/`. Пользователь без root с UID 10001, каталоги `/tmp/asr` и `/models` принадлежат ему. `CMD ["python", "-m", "ecoprofi.asr.worker"]`.
- [X] T005 [P] Создать `compose.asr.yaml` (`name: ecoprofi-asr`): сервис `asr-worker`, `build: {dockerfile: Dockerfile.asr}`, `restart: unless-stopped`, `deploy.resources.reservations.devices` с `driver: nvidia, count: all, capabilities: [gpu]`, секрет `asr_worker_token` из файла `secrets/asr_worker_token` (монтируется в `/run/secrets/asr_worker_token`), тома `asr-models:/models` и `asr-tmp:/tmp/asr`, переменные `ASR_SERVER=https://miniapp.bot-alimony.ru/asr/v1/`, `ASR_MODEL=large-v3`, `ASR_COMPUTE_TYPE=float16`. Ключ не передаётся через `environment`. Добавить `secrets/` в `.gitignore`, если его там нет.
- [X] T006 [P] Создать `deploy/asr-location.inc`: `location /asr/v1/` с `limit_req zone=asr burst=10 nodelay` (зона `limit_req_zone $binary_remote_addr zone=asr:1m rate=2r/s` объявляется в `deploy/vps/miniapp-nginx.conf` на уровне `http`), `client_max_body_size 1m`, `access_log off`, `proxy_pass http://asr-gateway:8100`. Вложенные location: `~ ^/asr/v1/jobs/[^/]+/result$` с `client_max_body_size 16m`; `~ ^/asr/v1/jobs/[^/]+/audio$` с `proxy_buffering off`. Заголовок `Authorization` передаётся как есть.
- [X] T007 Подключить `include /etc/nginx/conf.d/asr-location.inc;` в server-блок 443 файла `deploy/vps/miniapp-nginx.conf` (рядом с `include /etc/nginx/conf.d/miniapp-server.inc;` в строке 48 или 55 — блок с сертификатом Let's Encrypt), добавить `limit_req_zone` из T006. В `compose.vps.yaml` смонтировать `deploy/asr-location.inc` в шлюз как `/etc/nginx/conf.d/asr-location.inc:ro`. Зависит от T006.
- [X] T008 В `compose.yaml` добавить сервис `asr-gateway` на якоре `*indexer` с `profiles: [daemon]`, `command: ["python", "-m", "uvicorn", "ecoprofi.asr.gateway:app", "--host", "0.0.0.0", "--port", "8100"]`, `environment: *indexer-env`, томами `indexer-secrets:/secrets` и `asr-tmp:/tmp/asr`, `mem_limit: 300m`, `restart: unless-stopped`, `depends_on: db (service_healthy)`, сеть `internal`. Порт наружу не публикуется. Объявить том `asr-tmp` (`driver_opts` с `type: tmpfs` не использовать; лимит 2 ГБ контролирует `ecoprofi/asr/audio.py`, T028).

**Checkpoint:** `docker compose config -q` и `docker compose -f compose.asr.yaml config -q` проходят, образ `indexer` собирается с `ffmpeg`.

---

## Phase 2: Foundational — схема, задания, ключи, шлюз, окно

**Назначение:** общие части, без которых не работает ни одна история.

**⚠️ CRITICAL:** до конца фазы `asr-gateway` на VPS не запускается.

### Тесты Phase 2

- [X] T009 Создать `tests/asr/__init__.py` и `tests/asr/conftest.py`: переиспользовать фикстуры БД из `tests/integration/conftest.py` (DSN `test-db`, роли `ecoprofi_owner`/`ecoprofi_indexer`/`ecoprofi_bot`, применение `migrations/*.sql`), добавить фикстуры `asr_worker` (зарегистрированный распознаватель с известным ключом), `video_job` (запись `material` с `item_type = 'video'` и `asr_job` в `queued`), `FakeClock` и `TestClient` FastAPI поверх `ecoprofi.asr.gateway:app`.
  - *Реализация:* Сделано: `tests/asr/conftest.py` (свои фикстуры БД, `FakeVk`, `FakeRunner`, шлюз с подменами).
- [X] T010 [P] Тест миграции `tests/asr/test_migration_005.py`: таблицы `asr_worker`, `asr_schedule`, `asr_job`, `asr_transcript`, `asr_run` созданы; `asr_schedule` содержит ровно одну строку со значениями по умолчанию `weekdays = {7}`, `start_at = 01:00`, `window = 8 hours`, `catch_up = false`, `paused = false`, `initial_pass = 'pending'`, `silence_alert_days = 14`; `CHECK (speech_source IN ('subtitles', 'asr'))` отклоняет другое значение; существующие версии видео с `text_state = 'ready'` получили `speech_source = 'subtitles'`; для каждого `index_job` с `item_type = 'video' AND waiting_source` создано `asr_job` с `initial = true`, `state = 'queued'`, `source_version IS NULL`; вторая открытая строка `asr_run` (`finished_at IS NULL`) отклоняется уникальным индексом; `ecoprofi_bot` не может читать `asr_worker`.
  - *Реализация:* Сделано в `tests/asr/test_runs_status.py` (миграция и права).
- [X] T011 [P] Тест переходов `tests/asr/test_jobs.py`: все стрелки диаграммы data-model «Переходы состояния» — `queued → preparing → leased → transcribed`, `preparing → audio_unavailable (denied|no_file|deleted)`, `preparing → retry`, `leased → retry` по `failure`, `released / аренда истекла → queued` без роста `attempts`, `retry → queued` после `retry_at`; недопустимый переход (например, `published → leased`) поднимает исключение; смена `source_version` у `published`/`no_speech` возвращает в `queued` и обнуляет `attempts`, `failed_runs`.
  - *Реализация:* Сделано в `tests/asr/test_gateway_flow.py` и `test_runs_status.py`.
- [X] T012 [P] Тест ключей и аутентификации `tests/asr/test_workers_auth.py`: `issue_key` возвращает ключ из 256 бит один раз, в `asr_worker.token_hash` лежит `sha256` в hex, самого ключа в БД нет; запрос без ключа, с неверным и с отозванным ключом получает `401 {"error":{"code":"unauthorized"}}` с одинаковым телом; `revoke` возвращает действующие аренды распознавателя в `queued`.
  - *Реализация:* Сделано в `tests/asr/test_gateway_flow.py`.
- [X] T013 [P] Тест окна `tests/asr/test_window.py`: окно открыто при `initial_pass ≠ 'done'`; при `manual_requested_at`; внутри планового окна (вс 01:00–09:00 МСК при `weekdays = {7}`); закрыто в понедельник; `paused = true` закрывает окно во всех случаях, включая первичный проход, кроме внеочередного запуска по списку (`manual_only` не пуст): он открывается и при паузе, а `lease` выдаёт только записи из списка; `last_due_at` не даёт открыть второй запуск в том же плановом окне; `next_due_at` для четверга 2026-11-26 — `2026-11-29T01:00:00+03:00`.
- [X] T014 [P] Тест постановки в очередь индексатором `tests/asr/test_enqueue.py` (FR-01): проход индексатора для видео без пригодной дорожки создаёт `asr_job` (`initial = false`, `queued`, `source_version` вычислен из `duration` и `date` ответа `video.get`); у задания с `source_version IS NULL` (из миграции) индексатор заполняет `source_version` без сброса состояния и счётчиков; при изменении непустой `source_version` задание возвращается в `queued` со сбросом `attempts`, `failed_runs`, `lease_*`; задания в `cancelled` с причиной `subtitles` не возобновляются, пока у записи есть пригодные субтитры.
  - *Реализация:* Сделано в `tests/asr/test_indexer_link.py`.

### Реализация Phase 2

- [X] T015 Миграция `migrations/005_asr.sql` по data-model.md, владелец `ecoprofi_owner`:
  - *Реализация:* Имена `window_length`, `only_refs` (зарезервированные слова SQL); добавлены `audio_sha256`, `deadline_at`.
  - `ALTER TABLE material_version ADD speech_source text NULL CHECK (speech_source IN ('subtitles', 'asr'))`; `UPDATE` для версий видео с `text_state = 'ready'` → `'subtitles'`.
  - `asr_worker`: `id bigserial PK`, `name text UNIQUE NOT NULL`, `token_hash text UNIQUE NOT NULL`, `created_at timestamptz NOT NULL`, `revoked_at timestamptz NULL`, `last_seen_at timestamptz NULL`, `version text NULL`, `device jsonb NULL`.
  - `asr_schedule`: `id boolean PK DEFAULT true CHECK (id)`, `weekdays smallint[] NOT NULL DEFAULT '{7}'` с проверкой «ISO-дни 1–7, непустой массив», `start_at time NOT NULL DEFAULT '01:00'`, `"window" interval NOT NULL DEFAULT '8 hours'` с `CHECK` «от 30 минут до 24 часов», `catch_up boolean NOT NULL DEFAULT false`, `paused boolean NOT NULL DEFAULT false`, `initial_pass text NOT NULL DEFAULT 'pending'` с `CHECK (initial_pass IN ('pending','running','done'))`, `initial_started_at`, `initial_done_at`, `manual_requested_at`, `last_due_at` — `timestamptz NULL`, `manual_only text[] NULL` (ссылки `<owner>_<item>` внеочередного запуска по списку), `silence_alert_days integer NOT NULL DEFAULT 14`; `INSERT` одной строки.
  - `asr_run`: `id bigserial PK`, `worker_id bigint FK asr_worker`, `kind text NOT NULL CHECK (kind IN ('initial','scheduled','catch_up','manual'))`, `started_at timestamptz NOT NULL`, `until timestamptz NULL`, `finished_at timestamptz NULL`, `only text[] NULL` (список записей запуска по списку; `NULL` — вся очередь), `outcome text NULL CHECK (outcome IN ('queue_empty','window_closed','paused','worker_lost','resource_unavailable','error'))`, `taken`, `published`, `failed integer NOT NULL DEFAULT 0`, `audio_seconds`, `processing_seconds numeric NOT NULL DEFAULT 0`; частичный уникальный индекс `ON asr_run ((true)) WHERE finished_at IS NULL`.
  - `asr_job`: PK `(owner_id, item_id)`, `material_id bigint NOT NULL FK material`, `source_version text NULL` (`NULL` — версия источника ещё не определена: её заполняет индексатор (T023) или подготовка звука (T028); `result` принимается только при непустом значении), `state text NOT NULL CHECK (state IN ('queued','preparing','leased','transcribed','published','audio_unavailable','no_speech','retry','permanent','cancelled'))`, `reason text NULL CHECK (reason IN ('denied','no_file','deleted','timeout','decode','gpu_unavailable','server','subtitles','no_speech'))`, `initial boolean NOT NULL DEFAULT false`, `attempts integer NOT NULL DEFAULT 0`, `failed_runs integer NOT NULL DEFAULT 0`, `retry_at timestamptz NULL`, `lease_id uuid NULL`, `lease_worker_id bigint NULL FK asr_worker`, `lease_expires_at timestamptz NULL`, `run_id bigint NULL FK asr_run`, `audio_path text NULL`, `audio_bytes bigint NULL`, `duration_s integer NULL`, `updated_at timestamptz NOT NULL`; индекс по `(state, retry_at)`.
  - `asr_transcript`: `id bigserial PK`, `owner_id`, `item_id bigint` FK на `asr_job`, `source_version text NOT NULL`, `processor_version text NOT NULL`, `language text NOT NULL`, `segments jsonb NOT NULL`, `audio_seconds`, `processing_seconds numeric NOT NULL`, `version_id bigint NULL FK material_version`, `created_at timestamptz NOT NULL`, `UNIQUE (owner_id, item_id, source_version, processor_version)`.
  - Начальная очередь: `INSERT INTO asr_job (…, initial, state) SELECT … FROM index_job JOIN material … WHERE item_type = 'video' AND waiting_source` с `initial = true`, `state = 'queued'`, `source_version = NULL`. SQL-миграция не обращается к VK, а `duration` и `date` в каталоге не хранятся.
  - `GRANT SELECT, INSERT, UPDATE, DELETE` на все пять таблиц `TO ecoprofi_indexer`; `GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ecoprofi_indexer`. Новых прав `ecoprofi_bot` не выдаётся: `speech_source` читается через существующее право на `material_version`.
- [X] T016 [P] `ecoprofi/asr/workers.py`: `issue_key(conn, name) -> str` (`secrets.token_urlsafe(32)`, в БД `hashlib.sha256(key).hexdigest()`), `revoke(conn, name)` (ставит `revoked_at`, возвращает аренды этого распознавателя в `queued` с обнулением `lease_*`), `authenticate(conn, bearer) -> worker | None` (сравнение хешей через `hmac.compare_digest`, `revoked_at IS NULL`), `touch(conn, worker_id, version, device)` (обновляет `last_seen_at`, `version`, `device`). Ключ нигде не логируется.
- [X] T017 [P] `ecoprofi/asr/jobs.py`, базовая часть: функция `source_version(owner_id, item_id, duration, date) -> str` (sha256 строки из четырёх полей `video.get`); таблица допустимых переходов из data-model и функция `transition(conn, ref, to_state, *, reason=None, expected_lease=None)`, которая проверяет переход и `lease_id` в одной транзакции (`SELECT … FOR UPDATE`) и обновляет `updated_at`; `lease_next(conn, run_id, worker_id, now)` — берёт одно задание `queued` (или `retry` с `retry_at ≤ now`, или `audio_unavailable` с `updated_at ≤ now − AUDIO_RECHECK`) через `FOR UPDATE SKIP LOCKED`, порядок: `initial DESC, updated_at`; если у запуска заполнен `asr_run.only`, выдаются только записи из списка и в любом незавершённом состоянии из перечисленных; ставит `preparing`, новый `lease_id` (UUID4), `lease_expires_at = now + LEASE_TTL`, `run_id`; не выдаёт второе задание, пока у распознавателя есть действующая аренда (одно задание за раз); `expire_leases(conn, now)` — истёкшие аренды → `queued` без роста `attempts`, удаление `audio_path`. Короткий вид `lease_id` для журналов — первые 8 символов (`short_lease()`).
- [X] T018 [P] `ecoprofi/asr/window.py`: `is_open(schedule, now) -> (bool, kind | None, until | None)` по правилу data-model «Окно открыто, если»: непустой `manual_only` → `manual`, `until = now + window`, открывается и при паузе; иначе пауза закрывает всё; затем `initial_pass ≠ 'done'` → `initial`, `until = NULL`; `manual_requested_at` → `manual`, `until = now + window`; плановое окно (`weekdays`, `start_at` в зоне `Europe/Moscow`, `window`) с `last_due_at` раньше начала окна → `scheduled`, `until = начало + window`; `catch_up` и пропущенное окно в пределах «окно + 24 ч» → `catch_up`. `next_due_at(schedule, now)` для ответа `session`. Использовать `ecoprofi/duetime.py`, если там есть подходящие функции расчёта времени по МСК.
- [X] T019 `ecoprofi/asr/jobs.py`, запуски: `open_or_get_run(conn, worker_id, now)` — если открытый `asr_run` есть, вернуть его; иначе, если `window.is_open`, создать `asr_run` с `kind`, `until`, перенести `manual_only` в `asr_run.only`, сбросить `manual_requested_at` и `manual_only`, для `scheduled` записать `last_due_at`, для `initial` выставить `initial_pass = 'running'` и `initial_started_at` (если ещё не выставлены); `finish_run(conn, run_id, outcome)`; `close_lost_runs(conn, now)` — открытый запуск без `session`/`heartbeat` от распознавателя дольше `LEASE_TTL` закрывается с `outcome = 'worker_lost'`. Зависит от T017, T018.
- [X] T020 `ecoprofi/asr/gateway.py`, каркас FastAPI: `app = FastAPI()` с префиксом `/asr/v1`, зависимость `require_worker` (Bearer → `workers.authenticate`, иначе `401 {"error":{"code":"unauthorized"}}`), обработчик ошибок в формате `{"error":{"code","message","retryable"}}` как у шлюза Mini App (`ecoprofi/bot/miniapp`), пул соединений psycopg под ролью `ecoprofi_indexer` (`ecoprofi/db.py`), фоновая задача каждые 60 секунд: `expire_leases`, `close_lost_runs`. Фильтр журналов uvicorn: не писать заголовок `Authorization`, полные `lease_id` и строки с `okcdn|vkuser|vkvd|sig=|access_token`. Зависит от T016, T017, T019.
- [X] T021 `POST /asr/v1/session` и `POST /asr/v1/runs/{run_id}/finish` в `ecoprofi/asr/gateway.py` по contracts/asr-api.md: `session` вызывает `workers.touch`, затем `open_or_get_run` и отвечает `{"run": {"id","kind","until"} | null, "paused", "next_due_at"}`; если `device.vram_free_mb` меньше порога модели (4500 МБ для `float16`, 3000 МБ для `int8_float16`), запуск не открывается, а открытый закрывается с `resource_unavailable`. `finish` принимает `outcome ∈ {queue_empty, window_closed, paused, error}` и вызывает `finish_run`. Зависит от T020.
- [X] T022 Команды оператора в `ecoprofi/asr/__main__.py` (argparse, формат вывода и флаг `--json` как у `python -m ecoprofi.indexer`): `worker add <имя>` (печатает ключ один раз), `worker revoke <имя>`, `pause`, `resume`, `run-now [--only <owner>_<item>[,<owner>_<item>…]]` (ставит `manual_requested_at = now()`, а с `--only` — ещё `manual_only`, если открытого `asr_run` нет; иначе сообщает, что запуск уже идёт; несуществующая ссылка — ошибка без изменений). Запуск по списку проходит и при паузе: так проба (T037) распознаёт только выбранные записи, не открывая первичный проход. Ни одна команда, кроме `worker add`, не печатает ключ. Зависит от T016, T019.
- [X] T023 Постановка в очередь индексатором (FR-01, R7), чтобы новые записи без субтитров попадали в очередь с первого развёртывания:
  - *Реализация:* Постановка и отмена — в `ecoprofi/indexer/publish.py` (та же транзакция публикации), не в `update.py`.
  - `ecoprofi/indexer/vk_sources.py`: добавить в `VideoSource` поля `duration: int | None` и `date: int | None` из ответа `video.get` (разбор в `post_videos` и `_collect`), значения по умолчанию `None`, чтобы существующие вызовы не ломались;
  - `ecoprofi/asr/jobs.py`: `enqueue(conn, material_id, source: VideoSource)` — upsert в `asr_job` по `(owner_id, item_id)` с `source_version(owner_id, video_id, duration, date)`; при `NULL` в БД — только заполнить `source_version`; при изменении непустого значения — `queued` со сбросом `attempts`, `failed_runs`, `lease_*`; `cancelled / subtitles` не трогать;
  - `ecoprofi/indexer/update.py`, `update_worklist`, ветка видео без пригодной дорожки (`waiting_source`): вызвать `ecoprofi.asr.jobs.enqueue`. Из `ecoprofi.asr` импортируются только `jobs.source_version` и `jobs.enqueue`, без зависимостей шлюза.
  Зависит от T015, T017. Существующие тесты `tests/test_vk_sources.py` и `tests/integration/test_update_diff.py` должны проходить.

**Checkpoint:** `pytest tests/asr/test_migration_005.py tests/asr/test_jobs.py tests/asr/test_workers_auth.py tests/asr/test_window.py tests/asr/test_enqueue.py` проходит. Миграция применяется на копии БД без ошибок.

---

## Phase 3: User Story 1 — звук записи можно получить и распознать (P1) 🎯 MVP

**Цель:** вся цепочка «сервер получает звук → ПК распознаёт → сервер сохраняет расшифровку с таймкодами» работает на двух записях, одна из платной ленты клуба.

**Самостоятельная проверка:** quickstart §1–§2. Протокол пробы: дата, среда, способ доступа, материал, длительность, время распознавания, три сверенных отрезка на запись, открытый вопрос.

### Тесты US1

- [X] T024 [P] [US1] Тест подготовки звука `tests/asr/test_audio.py`: выбор потока из ответа `video.get` (приоритет — отдельная звуковая дорожка HLS/DASH, затем `files.mp4_*` минимального качества); команда `ffmpeg` содержит `-vn -ac 1 -ar 16000 -c:a libopus -b:a 24k` и запускается под `nice -n 10`; на фикстуре `tests/fixtures/speech_sample.mp4` (генерируется `ffmpeg -f lavfi` в conftest, 10 секунд синуса) получается `audio/ogg` с непустым `X-Audio-Sha256`; ответы VK «доступ запрещён», «нет файла», «видео удалено» дают `audio_unavailable` с `reason = denied | no_file | deleted`; таймаут источника даёт `retry` с `reason = server`; временный адрес не появляется в `caplog`; при превышении 2 ГБ на `asr-tmp` подготовка откладывается.
- [X] T025 [P] [US1] Тест API задания `tests/asr/test_gateway_jobs.py` по contracts/asr-api.md: `lease` → `200` с `lease_id`, `deadline_at`, `audio.href`; `204` при пустой очереди; `409 window_closed` при паузе; `audio` → `202` с `Retry-After: 15`, пока звук готовится, затем `200`, `206` на `Range`, `Content-Length` и `X-Audio-Sha256`; `410 audio_unavailable`; `heartbeat` продлевает `lease_expires_at` на `LEASE_TTL` и отвечает `continue: true`; `result` с чужим `audio_sha256`, пустым `segments` или `start_ms ≥ end_ms` → `422 invalid_result` и задание в `retry` с `reason = decode`; корректный `result` (gzip) → `202 {"state":"transcribed"}`, строка в `asr_transcript`, файл звука удалён, `audio_path = NULL`; `failure` с `decode | timeout | internal` → `retry`, `attempts + 1`; `gpu_unavailable` → `queued` без расхода попыток и запуск закрыт с `resource_unavailable`; `release` → `queued` без расхода попыток; задание дольше `DEADLINE` → `retry` с `reason = timeout`.
  - *Реализация:* Сделано в `tests/asr/test_gateway_flow.py`.
- [X] T026 [P] [US1] Тест распознавателя `tests/asr/test_worker_client.py` (без GPU, `transcribe` подменён): цикл `session → lease → audio (202, затем 200) → проверка sha256 → heartbeat → result → удаление файлов`; при старте удаляется всё в `ASR_TMP` (FR-15); при сетевой ошибке задержки растут от 5 секунд до 5 минут; при `409 lease_invalid` на `heartbeat` работа прерывается и файлы удаляются; ключ читается только из `/run/secrets/asr_worker_token`; в журнале нет ключа и полного `lease_id`.

### Реализация US1

- [X] T027 [P] [US1] `ecoprofi/indexer/vk_sources.py`: метод `VkSources.media_streams(source: VideoSource) -> MediaStreams | None` — свежий вызов `video.get` с доступом оператора и разбор полей `files` (`mp4_144`…`mp4_1080`), `hls`, `dash_*`, а также признак отдельной звуковой дорожки. Вернуть адреса только в памяти, без журналирования (как `fresh_track_url`). `None` и отдельные исключения для «доступ запрещён», «нет файла», «удалено». Добавить случаи в `tests/test_vk_sources.py`.
- [X] T028 [US1] `ecoprofi/asr/audio.py`: `prepare(conn, vk, job) -> AudioResult` — `media_streams` → выбор потока по правилу T024 → `subprocess` `nice -n 10 ffmpeg -nostdin -loglevel error -i <url> -vn -ac 1 -ar 16000 -c:a libopus -b:a 24k <asr-tmp>/<lease8>.ogg` с потоковым чтением и тайм-аутом; адрес передаётся только в аргументах процесса, stderr `ffmpeg` фильтруется от адресов перед записью в журнал; при истёкшем адресе — один повтор со свежим; запись `audio_path`, `audio_bytes`, `duration_s`, sha256; `source_version` вычисляется из `duration` и `date` того же свежего ответа `video.get` и записывается, если в задании `NULL` (задания из миграции); если непустое значение не совпало — задание возвращается в `queued` со сбросом счётчиков, звук удаляется; переход `preparing → leased` или `audio_unavailable(reason)` / `retry(server)`. `cleanup(conn)` удаляет файлы без действующей аренды и всё на томе при старте `asr-gateway`. Лимит тома 2 ГБ: при превышении задание остаётся в `preparing` до освобождения места. Доступ оператора — через `ecoprofi.indexer.access.Access` с тем же томом `/secrets` (продление защищено существующим `pg_advisory_lock`). Причина `deleted` означает только отказ при получении звука, а не подтверждённое удаление: `asr-gateway` не снимает запись из поиска и не меняет `material.state`. Удаление подтверждает индексатор по правилам 003 (T055). Зависит от T027.
- [X] T029 [US1] Эндпоинты `POST /asr/v1/jobs/lease`, `GET /asr/v1/jobs/{lease_id}/audio`, `POST /asr/v1/jobs/{lease_id}/heartbeat`, `/result`, `/failure`, `/release` в `ecoprofi/asr/gateway.py` строго по contracts/asr-api.md (коды, тела, `Retry-After: 15`, `Range`/`206`, `X-Audio-Sha256`, gzip для `result`). `lease` запускает `audio.prepare` фоновой задачей (одно задание за раз). `result` проверяет `audio_sha256`, интервалы `0 ≤ start_ms < end_ms`, непустой массив и сохраняет `asr_transcript` (`processor_version`, `language`, `segments`, `audio_seconds`, `processing_seconds`); повтор того же результата → `200` с текущим состоянием без второй строки. Учёт `taken`, `failed`, `audio_seconds`, `processing_seconds` в `asr_run`. `failure`: после `ATTEMPTS_PER_RUN` попыток в запуске задание ставится в `retry` с `retry_at` после конца запуска и `failed_runs + 1`. Зависит от T021, T028.
- [X] T030 [P] [US1] `ecoprofi/asr/worker/transcribe.py`: загрузка `faster_whisper.WhisperModel("large-v3", device="cuda", compute_type=ASR_COMPUTE_TYPE, download_root="/models")`, `transcribe(path, language="ru", vad_filter=True)` → список `{start_ms, end_ms, text, avg_logprob, no_speech_prob, compression_ratio}`; колбэк прогресса `processed_s` для heartbeat; `vram()` через `nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv` для `session`; при ошибке CUDA/нехватке памяти — исключение `GpuUnavailable`.
- [X] T031 [US1] `ecoprofi/asr/worker/client.py` и `ecoprofi/asr/worker/__main__.py`: httpx-клиент с `Authorization: Bearer` из `/run/secrets/asr_worker_token`, экспоненциальные повторы `CLIENT_BACKOFF`; цикл по разделу «Поведение распознавателя» contracts/asr-api.md: очистка `/tmp/asr` при старте → `session` каждые `SESSION_EVERY` → `lease` → `audio` (ожидание `Retry-After`, докачка `Range`, проверка `X-Audio-Sha256`) → распознавание в отдельном потоке с `heartbeat` каждые `HEARTBEAT_EVERY` → `result` (gzip) → удаление звука и расшифровки → следующий `lease` до `204` (`finish queue_empty`) или `409 window_closed`. `continue: false` → прервать, `release`. `GpuUnavailable` → `failure gpu_unavailable`. SIGTERM → `release` текущего задания. Журнал: «на связи», «задание <lease8> взято», «результат отправлен», без адресов и ключа. Зависит от T030.
- [X] T032 [US1] Команда `probe <owner>_<item>` в `ecoprofi/asr/__main__.py`: выполнить `audio.prepare` для одной записи без аренды, напечатать какие поля медиа вернул `video.get` (`files`, `hls`, `dash`; только имена полей и качества, без адресов), есть ли отдельная звуковая дорожка, размер файла, длительность, время подготовки; удалить файл по окончании. Также команда `jobs [--state <состояние>]` — список заданий с `reason`, без адресов (нужна для выбора записей пробы). Зависит от T028.
- [X] T033 [US1] `deploy/vps-deploy.py`: включить в сборку и перенос обновлённый образ `indexer` (с `ffmpeg`) и перезапуск сервисов `asr-gateway` и `miniapp-gateway` (после `nginx -t` внутри контейнера шлюза). Если `nginx -t` не прошёл — остановиться без перезагрузки шлюза.
- [X] T034 [US1] Создать `specs/006-video-asr-transcriber/acceptance.md` с разделами quickstart §0–§7 и пустыми полями принципа V.
- [ ] T035 [US1] [Ч] `[VPS]` Развернуть: `deploy/vps-deploy.py`, применить миграцию `005_asr.sql`, **сразу** `$ASR pause` (иначе начнётся первичный проход до пробы), проверить `asr-gateway` в Up, `nginx -t`, порты наружу только 22/80/443 (quickstart §0). Записать в acceptance.md.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 3 (деплой на VPS).
- [ ] T036 [US1] [Ч] `[ПК]` Проверка GPU и регистрация распознавателя (quickstart §0–§1): `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`; остановить стенд ПК из 005; `$ASR worker add owner-pc`, ключ в `secrets/asr_worker_token`; `docker compose -f compose.asr.yaml up -d`; в журнале «на связи»; `curl` с ключом `x` → `401`. Записать модель GPU и объём видеопамяти.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаги 1 и 4.
- [ ] T037 [US1] [Ч] Проба (quickstart §2, SC-01): выбрать две записи из `$ASR jobs --state queued` (одна открытая, одна из платной ленты клуба), `$ASR probe` по каждой; записать поля медиа, наличие звуковой дорожки, время подготовки, размер, пик ЦП/RAM VPS по `docker stats`; затем `$ASR run-now --only <ref1>,<ref2>` при сохранённой паузе — распознаватель берёт только эти две записи, первичный проход не начинается; дождаться `transcribed` и `queue_empty` у запуска; пауза остаётся до T051; по каждой записи — длительность звука, время распознавания, три отрезка, сверенных с плеером (±5 с); пустые тома `asr-tmp` на ПК и VPS; запись с недоступным звуком — `audio_unavailable` с причиной. Если `video.get` не отдаёт медиа для клубной записи — зафиксировать открытый вопрос владельцу (запасной вариант B только его решением).
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 5.

**Checkpoint:** протокол пробы заполнен, `pytest tests/asr/test_audio.py tests/asr/test_gateway_jobs.py tests/asr/test_worker_client.py` проходит. Расшифровки сохраняются, но в поиск ещё не попадают.

---

## Phase 4: User Story 2 — найти эпизод в записи без субтитров (P1)

**Цель:** расшифровка становится эпизодами с таймкодами в той же версии записи, участник получает выдержку с пометкой «речь распознана автоматически» и ссылку на минуту.

**Самостоятельная проверка:** quickstart §4, SC-02 — три вопроса по контрольной записи, эпизод в первой тройке, плеер на нужном месте; для неподписчика и при сбое проверки подписки выдачи нет.

### Тесты US2

- [X] T038 [P] [US2] Тест публикатора `tests/asr/test_publish.py`: отрезки с высоким `no_speech_prob` и высоким `compression_ratio` отброшены (пороги — константы в `ecoprofi/asr/publish.py`, по умолчанию `no_speech_prob > 0.6` и `compression_ratio > 2.4`); повторяющиеся соседние отрезки не дублируются в выдержке (правила 001 через `build_episodes`); меньше `MIN_SPEECH_CHARS` после фильтра → задание `no_speech` (`reason = no_speech`), версия не создаётся, карточка остаётся; успешная публикация создаёт `material_version` с `speech_source = 'asr'`, `processor_version = 'asr-1/large-v3'`, `content_hash = sha256(source_version)`, фрагменты эпизодов с `recognized = true`, переключает `published_version_id`, ставит `asr_transcript.version_id` и `asr_job.state = 'published'`; ошибка эмбеддингов оставляет прежнюю опубликованную версию и ставит `retry`; фрагменты наследуют аудиторию `material`.
  - *Реализация:* Сделано в `tests/asr/test_publish_filter.py` и `test_gateway_flow.py`; сбой публикации оставляет `transcribed` с `retry_at`.
- [X] T039 [P] [US2] Интеграционный тест выдачи `tests/asr/test_search_recognized.py` (по образцу `tests/integration/test_episode_search.py` и `test_audience.py`): опубликованная распознанная запись находится вопросом по её тексту, карточка содержит эпизод с интервалом и подписью «речь распознана автоматически»; клубная распознанная запись не выдаётся неподписчику и при недоступной проверке подписки.
  - *Реализация:* Сделано: `tests/integration/test_asr_search.py`.
- [X] T040 [P] [US2] Тест защиты распознанной версии `tests/asr/test_card_guard.py`: после публикации распознанной версии проход индексатора по той же записи без пригодной дорожки (`waiting_source`) не создаёт версию-карточку и не переключает `published_version_id`, число фрагментов `recognized = true` не меняется; при изменившейся `source_version` (другие `duration`/`date`) распознанная версия остаётся в поиске, пока не опубликована новая, а задание возвращается в `queued`.
  - *Реализация:* Сделано в `tests/asr/test_indexer_link.py`.

### Реализация US2

- [X] T041 [US2] `ecoprofi/asr/publish.py`: `publish_transcript(conn, ai, transcript_id)` — фильтр отрезков (пороги T038) → преобразование в `ecoprofi.indexer.subtitles.Cue` → `build_episodes` → эмбеддинги через тот же клиент `ai`, что в `ecoprofi/indexer/publish.py` → в одной транзакции с `pg_advisory_xact_lock` по записи (как `_lock` в `ecoprofi/indexer/publish.py`) создать `material_version` (`speech_source = 'asr'`, `processor_version = PROCESSOR_VERSION`, `content_hash = sha256(source_version)`; существующий `UNIQUE` не даёт дубля) и фрагменты `recognized = true`, затем `_switch`. Переиспользовать вспомогательные функции `ecoprofi/indexer/publish.py` (вынести общие в публичные, не копировать). Если запись ждёт решения редактора об аудитории — публиковать по тем же правилам, что и прочие материалы (не выдаётся до решения).
  - *Реализация:* Фильтр: `compression_ratio > 2.4` или (`no_speech_prob > 0.6` и `avg_logprob < -1.0`).
- [X] T042 [US2] Фоновый публикатор в `ecoprofi/asr/gateway.py`: после `202` на `result` поставить `publish_transcript` в очередь фоновой задачи; при старте `asr-gateway` доопубликовать задания в `transcribed`. Ответ повторного `result` — `200` с `published` или `transcribed`. Учёт `published` в `asr_run`. Зависит от T029, T041.
- [X] T043 [P] [US2] `ecoprofi/bot/cards.py`: в `_place` для `kind == "episode"` при `card.get("recognized")` добавлять « · речь распознана автоматически» к «Эпизод <интервал>»; убедиться, что `recognized` доходит до карточки эпизода из `ecoprofi/bot/search.py` (поле уже выбирается в SQL, строка 95) и в ответ Mini App. Подпись для страниц документов («текст распознан») не меняется. Обновить ожидания в `tests/fixtures/expectations.json`, если они затронуты.
- [X] T044 [US2] Защита распознанной версии в `ecoprofi/indexer/publish.py` (R7; без неё первый же плановый проход индексатора в 07:00 или 19:00 затрёт распознанные эпизоды карточкой): в `publish_version` при `track is None` не публиковать карточку и не переключать версию, если у `material` опубликована версия с `speech_source = 'asr'`; задание индексатора остаётся с `waiting_source`, как сейчас. Новая версия-карточка не создаётся и при изменившейся `source_version`: распознанная версия остаётся до публикации новой (T023 возвращает задание в `queued`). Существующие тесты `tests/integration/test_publication.py` должны проходить. Зависит от T023, T041.
- [ ] T045 [US2] [Ч] Поиск по распознанной записи (quickstart §4, SC-02): после публикации контрольной записи из пробы — три вопроса под ролью подписчика в Mini App, эпизод в первой тройке, подпись видна, ссылка открывает плеер на эпизоде, звучащий фрагмент подтверждает выдержку; под ролью неподписчика и при сбое проверки подписки выдачи нет. Записать в acceptance.md.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 6.

**Checkpoint:** US1 и US2 вместе дают поиск по речи контрольной записи. `pytest tests/asr/test_publish.py tests/asr/test_search_recognized.py` проходит.

---

## Phase 5: User Story 3 — распознавание по своему, более редкому графику (P1)

**Цель:** первичный проход сразу после развёртывания, затем недельный график вс 01:00 МСК, окно 8 ч; пауза, возобновление и внеочередной запуск; график индексатора не меняется.

**Самостоятельная проверка:** quickstart §3 и §5 — первичный проход начат не позже 15 минут после развёртывания и завершён с исходом у каждой записи; `schedule set` не меняет `python -m ecoprofi.indexer schedule show`, и наоборот; неделя наблюдения.

### Тесты US3

- [X] T046 [P] [US3] Тест первичного прохода и запусков `tests/asr/test_runs.py`: при `initial_pass = 'pending'` первый `session` открывает запуск `kind = 'initial'`, `until = NULL`, ставит `running` и `initial_started_at`; после того как у всех `initial = true` итоговый исход (`published`, `audio_unavailable`, `no_speech`, `permanent`, `cancelled`) — `initial_pass = 'done'`, `initial_done_at`, следующий `session` вне планового окна получает `run: null`; потерянный распознаватель (нет связи дольше `LEASE_TTL`) — запуск закрыт `worker_lost`, задание в `queued`, следующий `session` продолжает первичный проход; второй открытый запуск невозможен; `run-now` при идущем запуске не создаёт второго; при `until` в прошлом `heartbeat` отвечает `continue: false`, `lease` — `409 window_closed`, незавершённое задание возвращается в `queued` без частичной версии; пропущенный плановый запуск при `catch_up = false` не догоняется, при `catch_up = true` догоняется в пределах «окно + 24 ч» и только один раз.
  - *Реализация:* Сделано в `tests/asr/test_runs_status.py`.
- [X] T047 [P] [US3] Тест независимости графиков `tests/asr/test_schedule_independent.py`: `schedule set` распознавания не меняет строку `run_schedule`, изменение графика индексатора (`ecoprofi.indexer.schedule`) не меняет `asr_schedule`; валидация `--days` (ISO 1–7, непустой), `--window` (от 30 минут до 24 часов), `--at` (HH:MM).
  - *Реализация:* Сделано в `tests/asr/test_runs_status.py`.

### Реализация US3

- [X] T048 [US3] `ecoprofi/asr/jobs.py`: `check_initial_done(conn, now)` — после каждого итогового перехода, если нет заданий `initial = true` вне `published | audio_unavailable | no_speech | permanent | cancelled`, выставить `initial_pass = 'done'` и `initial_done_at`, закрыть запуск `initial` с `queue_empty`. Вызывать из `transition` при итоговых состояниях. Зависит от T019.
- [X] T049 [US3] Закрытие окна посреди задания в `ecoprofi/asr/gateway.py`: `heartbeat` отвечает `continue: false`, а `lease` — `409 window_closed`, если пауза или `now ≥ run.until`; фоновая задача закрывает запуск с `window_closed` или `paused`, когда активных аренд не осталось. Частичный результат не публикуется (результат по аренде после `release` → `409 lease_invalid`). Зависит от T029.
- [X] T050 [US3] Команды `schedule show` и `schedule set --days 7 --at 01:00 --window 8h [--catch-up on|off]` в `ecoprofi/asr/__main__.py`: `show` печатает график распознавания и рядом график индексатора из `run_schedule` (только чтение), состояние первичного прохода и `next_due_at`; `set` меняет только `asr_schedule` с валидацией T047. Пример вывода — contracts/operator-commands.md.
- [ ] T051 [US3] [Ч] Первичный проход (quickstart §3, SC-03): `$ASR resume` после пробы; «Первичный проход: идёт» не позже 15 минут после развёртывания; выключить ПК посреди записи, через 30 минут включить — `worker_lost`, задание вернулось, проход продолжился, частичной версии в поиске нет; по завершении — «Первичный проход: завершён», итог по исходам в протокол, `schedule show` — следующий запуск вс 01:00 МСК.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 7.
- [ ] T052 [US3] [Ч] Неделя наблюдения графиков (quickstart §5, SC-04, SC-08): индексатор в 07:00 и 19:00, не больше двух опросов в сутки; распознавание только вс с 01:00, начало не позже 01:15 при включённом ПК; неделя с выключенным ПК — нет оповещений о сбое распознавания, Mini App и поиск работают. Записать в acceptance.md.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 7.

**Checkpoint:** первичный проход и недельный график работают, `pytest tests/asr/test_runs.py tests/asr/test_schedule_independent.py` проходит.

---

## Phase 6: User Story 4 — не переделывать сделанное, заменять расшифровку субтитрами (P2)

**Цель:** индексатор ставит и снимает задания, не затирает распознанную версию карточкой, субтитры заменяют распознанные эпизоды атомарно; повторы не создают дублей; переобработка по версии распознавателя.

**Самостоятельная проверка:** quickstart §6, SC-05, SC-06 — `run-now` дважды не меняет число версий и эпизодов; запись с появившимися субтитрами после прохода индексатора имеет `speech_source = subtitles`, задание `cancelled / subtitles`, один комплект эпизодов.

### Тесты US4

- [X] T053 [P] [US4] Тест взаимодействия с индексатором `tests/asr/test_indexer_link.py` (постановка в очередь проверяется в T014, защита от карточки — в T040): появление субтитров публикует версию `speech_source = 'subtitles'`, задание → `cancelled`, `reason = subtitles`, `lease_id = NULL`; последующий `heartbeat`/`result` по этой аренде → `409 lease_invalid`; в поиске один комплект эпизодов; подтверждённое индексатором удаление записи (`unpublish_deleted`) ставит заданию `cancelled` с `reason = deleted`, а `audio_unavailable / deleted` от `asr-gateway` запись из поиска не снимает.
  - *Реализация:* Сделано в `tests/asr/test_indexer_link.py`.
- [X] T054 [P] [US4] Тест идемпотентности `tests/asr/test_idempotent.py`: два запуска подряд не берут `published` записи, число `material_version` и `fragment` не меняется; повторный `result` с тем же содержимым не создаёт второй `asr_transcript` (UNIQUE `(owner_id, item_id, source_version, processor_version)`) и второй версии; результат по отменённой, истёкшей или переданной другому распознавателю аренде → `409 lease_invalid` (FR-08); ошибка публикации оставляет прежнюю версию в поиске.
  - *Реализация:* Сделано в `tests/asr/test_gateway_flow.py`.

### Реализация US4

- [X] T055 [US4] `ecoprofi/indexer/publish.py`: в `publish_version` для видео с субтитрами ставить `speech_source = 'subtitles'`, после публикации — `asr_job.state = 'cancelled'`, `reason = 'subtitles'`, `lease_id = NULL` и удаление `audio_path`; в `unpublish_deleted` для видео — `asr_job.state = 'cancelled'`, `reason = 'deleted'`, `lease_id = NULL`. Защита распознанной версии от карточки уже сделана в T044. Существующие тесты `tests/integration/test_publication.py` должны продолжать проходить.
- [X] T056 [US4] Команда `reprocess --processor-version <версия>` в `ecoprofi/asr/__main__.py`: задания `published` и `no_speech`, у которых `asr_transcript.processor_version` отличается от указанной, → `queued` со сбросом счётчиков; печать числа поставленных. Прежняя версия остаётся в поиске до новой публикации.
- [ ] T057 [US4] [Ч] Повторы и субтитры (quickstart §6): `$ASR run-now` дважды — число версий и эпизодов не изменилось; контрольная запись, для которой владелец добавил субтитры, после ближайшего прохода индексатора имеет `speech_source = subtitles`, задание `cancelled / subtitles`, один комплект эпизодов. Записать в acceptance.md.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 8.

**Checkpoint:** `pytest tests/asr/test_indexer_link.py tests/asr/test_idempotent.py tests/integration` проходит.

---

## Phase 7: User Story 5 — видеть очередь и исход распознавания (P3)

**Цель:** сводка по исходам и скорости, список заданий, возврат в очередь, оповещения о постоянном сбое и долгом молчании ПК.

**Самостоятельная проверка:** `$ASR status` сверяется с каталогом; запись с тремя запусками сбоя подряд даёт одно оповещение `asr_permanent`; `requeue` закрывает его.

### Тесты US5

- [X] T058 [P] [US5] Тест сводки и оповещений `tests/asr/test_status_alerts.py`: `status --json` содержит счётчики по исходам FR-14 (распознано, ждёт, звук недоступен, речь не найдена, временный сбой, постоянный сбой, заменено субтитрами, снято после удаления); каждое состояние `asr_job` попадает ровно в один счётчик (`retry` — только «временный сбой»), первичный проход «сделано / всего», последний запуск и его исход, последнюю связь ПК, часы звука, время обработки, скорость (ч записи / ч работы); `failed_runs ≥ 3` → `permanent`, одно оповещение `asr_permanent:<owner>_<item>`, задание больше не выдаётся `lease`; `requeue` → `queued`, `failed_runs = 0`, оповещение закрыто; `max(last_seen_at)` старше `silence_alert_days` → одно `asr_silent`, без повторов, закрывается первым `session`; выключенный ПК в отдельный плановый запуск оповещения не вызывает.
  - *Реализация:* Сделано в `tests/asr/test_runs_status.py`.

### Реализация US5

- [X] T059 [US5] Переход в `permanent` в `ecoprofi/asr/jobs.py`: при `failed_runs ≥ PERMANENT_AFTER_RUNS` — `state = 'permanent'` и `ecoprofi.indexer.alerts.open_alert(conn, f"asr_permanent:{owner}_{item}", …)` с текстом новой функции `text_asr_permanent` в `ecoprofi/indexer/alerts.py`. `failed_runs` сбрасывается в 0 после успешной публикации.
- [X] T060 [US5] Оповещение `asr_silent` в фоновой задаче `ecoprofi/asr/gateway.py`: раз в час, если `max(asr_worker.last_seen_at)` старше `asr_schedule.silence_alert_days` — `open_alert(conn, "asr_silent", text_asr_silent(days))` (функция в `ecoprofi/indexer/alerts.py`); `session` закрывает `asr_silent` через `close_alert`. Доставка — существующим механизмом `alert` из 003.
- [X] T061 [US5] Команды `status`, `worker list` и `requeue <owner>_<item>` в `ecoprofi/asr/__main__.py` по contracts/operator-commands.md (пример вывода `status` — там же; `--json` для всех). `requeue` закрывает `asr_permanent:<owner>_<item>`. `worker list`: имя, время выпуска и отзыва, последняя связь, GPU из `device`; без ключей и хешей.
- [ ] T062 [US5] [Ч] Сводка (quickstart §2–§3): `$ASR status` сверить с каталогом (`$ASR jobs` по состояниям), записать скорость ПК (SC-09) в acceptance.md.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 8.

**Checkpoint:** `pytest tests/asr/test_status_alerts.py` проходит, сводка совпадает с каталогом.

---

## Phase 8: Polish — секреты, документация, итоговая приёмка

- [X] T063 [P] Тест отсутствия секретов `tests/asr/test_no_secrets.py` по образцу `tests/integration/test_no_secrets.py`: прогнать цикл lease → audio → result с подставным временным адресом `https://vkvd.okcdn.ru/?sig=…` и ключом; ни в `caplog` сервера, ни в журнале клиента, ни в ответах API, ни в выводе команд оператора нет адреса, ключа, `Bearer` и полного `lease_id`.
  - *Реализация:* Сделано в `tests/asr/test_gateway_flow.py`, `test_audio.py`, `test_worker_client.py`.
- [X] T064 [P] Обновить `specs/006-video-asr-transcriber/quickstart.md`, если реализация уточнила команды, пороги фильтра (T038) или пути; README `tests/fixtures/README.md` — фикстура `speech_sample.mp4`.
- [ ] T065 [P] Обновить архитектурный документ `Архитектура_бота_Экопрофи.docx` (обязательство plan.md «Порядок изменений»): раздел архитектуры — сервис `asr-gateway`, распознаватель на ПК, канал HTTPS `/asr/v1/`, звук через сервер; приложение В, критерий ASR-02 — отметка о реализации и ссылка на acceptance.md 006. Поправка конституции не требуется.
  - *Не сделано:* документ владельца не правился автоматически. Можно сделать на любом ПК; содержание правки — в описании задачи.
- [ ] T066 Полный прогон `docker compose --profile test run --rm --build tests` (все тесты проекта, включая `tests/asr`) — без падений.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 2.
- [ ] T067 [Ч] Журналы и секреты на обеих сторонах (quickstart §7, SC-07): `grep` по журналам `asr-gateway` и `indexer-daemon` на VPS даёт 0, `Select-String` по журналу `asr-worker` на ПК пуст; тома `asr-tmp` пусты после запуска. Записать в acceptance.md.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 8.
- [ ] T068 [Ч] Проверить ресурсы VPS во время подготовки звука: `asr-gateway` ≤ 300 МБ RAM, Mini App отвечает, поиск по остальным материалам работает. При нехватке — вопрос владельцу о варианте B (research R1). Записать в acceptance.md.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаги 3 и 8.
- [ ] T069 Итог в `specs/006-video-asr-transcriber/acceptance.md`: матрица SC-01…SC-09 с результатом и ссылкой на раздел протокола, открытые вопросы владельцу; обновить статус в шапке `tasks.md`.
  - **→ ПК с ASR:** [handoff-asr-pc.md](handoff-asr-pc.md), шаг 8.

---

## Зависимости и порядок выполнения

### Между фазами

```text
Phase 1 Setup ──▶ Phase 2 Foundational ──▶ US1 (P1, MVP) ──▶ US2 (P1)
                                          │                ├──▶ US4 (P2)
                                          └──▶ US3 (P1) ───┘
                                          └──▶ US5 (P3) (после T029)
                                                              └──▶ Phase 8 Polish
```

- **Phase 2** блокирует все истории: схема (T015), переходы и аренда (T017), окно (T018), шлюз (T020–T021), команды pause/run-now (T022), постановка в очередь индексатором (T023). T023 стоит здесь, а не в US4, чтобы записи без субтитров, появившиеся после развёртывания, попадали в очередь сразу (FR-01).
- **US1** — первая: без звука и распознавателя остальные истории нечем проверить на живых данных. Код US3 и US5 можно писать параллельно с US1 после Phase 2, но их задачи `[Ч]` идут после пробы T037.
- **US2** зависит от US1 (T029 — приём результата). T044 развёртывается вместе с публикатором T042, не позже: иначе плановый проход индексатора затрёт распознанные эпизоды карточкой.
- **US3** по коду зависит только от Phase 2 и T029 (T049); задача T051 идёт после T037 и T042, чтобы первичный проход сразу публиковал.
- **US4** зависит от US2 (публикатор T041, защита T044 и `speech_source`).
- **US5** зависит от T029 (учёт `failed_runs`).
- **Phase 8** — после всех историй; T065 обязателен до итоговой приёмки.

### Внутри истории

Тесты → модели и функции `jobs.py`/`window.py` → `audio.py`/`publish.py` → эндпоинты `gateway.py` → команды `__main__.py` → задачи `[Ч]`.

### Файлы с последовательным доступом

`ecoprofi/asr/gateway.py` (T020, T021, T029, T042, T049, T060), `ecoprofi/asr/jobs.py` (T017, T019, T048, T059) и `ecoprofi/asr/__main__.py` (T022, T032, T050, T056, T061), `ecoprofi/indexer/vk_sources.py` (T023, T027) и `ecoprofi/indexer/publish.py` (T044, T055) правятся несколькими задачами — их не распараллеливать между собой.

## Параллельные примеры

**Phase 1:** T002, T003, T004, T005, T006 — разные файлы.

**Phase 2:** после T009 — тесты T010, T011, T012, T013, T014 одновременно; затем T016, T017, T018 одновременно (после T015); T023 после T017.

**US1:**

```text
T024 tests/asr/test_audio.py
T025 tests/asr/test_gateway_jobs.py
T026 tests/asr/test_worker_client.py
T027 ecoprofi/indexer/vk_sources.py
T030 ecoprofi/asr/worker/transcribe.py
```

Серверная ветка (T027 → T028 → T029 → T032) и клиентская (T030 → T031) идут параллельно.

**US2:** T038, T039, T040, T043 одновременно; T041 → T042 и T041 → T044.

**US3:** T046, T047 одновременно; T048, T050 параллельно с T049.

**US4:** T053, T054 одновременно; T055 и T056 параллельно (разные файлы).

## Стратегия реализации

### MVP — только US1

1. Phase 1 и Phase 2.
2. US1: звук на сервере, распознаватель на ПК, расшифровка сохраняется.
3. **Стоп и проверка:** протокол пробы (T037). Это ответ на главный открытый вопрос — отдаёт ли `video.get` медиа для клубных записей и хватает ли ресурсов VPS и ПК. При отрицательном результате дальше не идти без решения владельца.

### Инкрементальная поставка

1. MVP (US1) → проба.
2. US2 → публикация и поиск по речи контрольной записи (ценность для участника).
3. US3 → `resume`, первичный проход по всей очереди, недельный график.
4. US4 → связь с индексатором: субтитры вытесняют распознавание, нет дублей.
5. US5 → сводка и оповещения.
6. Polish → секреты, архитектурный документ, итоговая приёмка.

Каждая история после своей фазы разворачивается на VPS через `deploy/vps-deploy.py` и не ломает предыдущие.
