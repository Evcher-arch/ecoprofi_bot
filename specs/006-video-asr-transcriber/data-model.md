# Модель данных 006: распознавание записей без субтитров

Миграция `migrations/005_asr.sql`. Владелец схемы — `ecoprofi_owner`. Права:

- `ecoprofi_indexer` — чтение и запись всех новых таблиц. От этой роли работают `indexer-daemon` и `asr-gateway`.
- `ecoprofi_bot` — только чтение `material_version.speech_source`, через существующее право на `material_version`.

Существующие сущности (`material`, `material_version`, `fragment`, `index_job`, `alert`) описаны в моделях данных [001](../001-find-lecture-episode/data-model.md)–[003](../003-autonomous-indexer/data-model.md).

## Изменения существующих таблиц

### `material_version`

| Поле | Тип | Правило |
| --- | --- | --- |
| `speech_source` | `text NULL` | `CHECK (speech_source IN ('subtitles', 'asr'))`. Заполняется только для записей с эпизодами. При миграции для существующих версий с `text_state = 'ready'` у видео ставится `'subtitles'` |

- **Распознанная версия:** `processor_version = 'asr-1/large-v3'`, `content_hash = sha256(source_version)`. Повтор с тем же ключом не создаёт второй версии (существующее `UNIQUE`).

### `fragment`

- Новых полей нет. У эпизодов распознанной версии `recognized = true`. Выдача (`ecoprofi/bot/cards.py`) добавляет к подписи эпизода «речь распознана автоматически».

## Новые таблицы

### `asr_worker` — зарегистрированный распознаватель

| Поле | Тип | Правило |
| --- | --- | --- |
| `id` | `bigserial PK` | |
| `name` | `text UNIQUE NOT NULL` | Например, `owner-pc` |
| `token_hash` | `text UNIQUE NOT NULL` | `sha256` ключа в hex. Значение ключа не хранится |
| `created_at` | `timestamptz NOT NULL` | |
| `revoked_at` | `timestamptz NULL` | Отозванный ключ получает `401` |
| `last_seen_at` | `timestamptz NULL` | Обновляется при каждом `session` |
| `version` | `text NULL` | Версия распознавателя из `session` |
| `device` | `jsonb NULL` | Модель GPU и объём видеопамяти — только для сводки |

### `asr_schedule` — график распознавания (одна строка)

| Поле | Тип | По умолчанию | Правило |
| --- | --- | --- | --- |
| `id` | `boolean PK` | `true` | `CHECK (id)` |
| `weekdays` | `smallint[] NOT NULL` | `{7}` | ISO-дни 1–7, непустой массив. 7 — воскресенье |
| `start_at` | `time NOT NULL` | `01:00` | Время МСК |
| `window_length` | `interval NOT NULL` | `8 hours` | От 30 минут до 24 часов. Имя `window` — зарезервированное слово SQL |
| `catch_up` | `boolean NOT NULL` | `false` | Догон пропущенного запуска при следующем выходе ПК на связь в течение того же окна + 24 ч |
| `paused` | `boolean NOT NULL` | `false` | Пауза автоматических запусков, включая первичный проход |
| `initial_pass` | `text NOT NULL` | `'pending'` | `pending` → `running` → `done` |
| `initial_started_at`, `initial_done_at` | `timestamptz NULL` | | |
| `manual_requested_at` | `timestamptz NULL` | | Внеочередной запуск, сбрасывается при начале запуска |
| `manual_only` | `text[] NULL` | | Внеочередной запуск по списку записей `<owner>_<item>` (`run-now --only`). Переносится в `asr_run.only_refs` и сбрасывается при начале запуска |
| `last_due_at` | `timestamptz NULL` | | Последнее начатое плановое окно; защищает от двойного запуска в одном окне |
| `silence_alert_days` | `integer NOT NULL` | `14` | Порог оповещения `asr_silent` |

**Окно открыто, если** запрошен внеочередной запуск по списку (`manual_only` не пуст) — независимо от паузы, это явное действие оператора для пробы. Запуск по списку выдаёт только записи из списка и не открывает первичный проход.

Иначе окно открыто, если не выставлена пауза `paused` и выполняется любое из условий:

- `initial_pass ≠ 'done'`;
- запрошен внеочередной запуск (`manual_requested_at`);
- текущий момент попадает в плановое окно;
- `catch_up` включён и пропущенное окно ещё не обработано.

### `asr_job` — задание распознавания

| Поле | Тип | Правило |
| --- | --- | --- |
| `owner_id`, `item_id` | `bigint`, PK вместе | Ключ записи: `material.item_type = 'video'` |
| `material_id` | `bigint NOT NULL FK material` | |
| `source_version` | `text NULL` | Хеш `owner_id`, `item_id`, `duration`, `date` из `video.get`. `NULL` — версия ещё не определена (задания из миграции: SQL не обращается к VK, а `duration` и `date` в каталоге не хранятся). Заполняется индексатором при ближайшем проходе или при подготовке звука из свежего `video.get`. Результат принимается только при непустом значении |
| `state` | `text NOT NULL` | См. переходы |
| `reason` | `text NULL` | `denied`, `no_file`, `deleted`, `timeout`, `decode`, `gpu_unavailable`, `server`, `subtitles`, `no_speech` |
| `initial` | `boolean NOT NULL DEFAULT false` | Входит в начальную очередь первичного прохода |
| `attempts` | `integer NOT NULL DEFAULT 0` | Попытки в текущем запуске |
| `failed_runs` | `integer NOT NULL DEFAULT 0` | Запуски подряд со сбоем. При `≥ 3` → `permanent` |
| `retry_at` | `timestamptz NULL` | Не раньше этого времени |
| `lease_id` | `uuid NULL` | Действующая аренда |
| `lease_worker_id` | `bigint NULL FK asr_worker` | |
| `lease_expires_at` | `timestamptz NULL` | |
| `run_id` | `bigint NULL FK asr_run` | |
| `audio_path` | `text NULL` | Путь временного файла на томе `asr-tmp`. Удаляется вместе с файлом |
| `audio_bytes`, `duration_s` | `bigint NULL`, `integer NULL` | |
| `audio_sha256` | `text NULL` | Контрольная сумма подготовленного звука: заголовок `X-Audio-Sha256` и проверка `result` |
| `deadline_at` | `timestamptz NULL` | Предел на запись: время подготовки звука + 1,5 × длительность + 15 мин. Превышение — `retry` / `timeout` |
| `updated_at` | `timestamptz NOT NULL` | |

**Уникальность:** одно задание на запись. Смена непустой `source_version` возвращает задание в `queued` и сбрасывает счётчики. Заполнение `NULL` состояние не меняет.

**Переходы состояния**

```text
queued ──lease──▶ preparing ──звук готов──▶ leased ──result──▶ transcribed ──публикатор──▶ published
   ▲                 │                        │                    │
   │                 ├─ отказ/нет файла ─▶ audio_unavailable (reason)   └─ мало речи ─▶ no_speech
   │                 └─ временный сбой ─▶ retry
   │                                          ├─ failure(временный) ─▶ retry ──retry_at──▶ queued
   │                                          ├─ released / аренда истекла ─▶ queued (attempts не растёт)
   └──────────── requeue оператора ◀──────────┴─ failed_runs ≥ 3 ─▶ permanent
любое незавершённое ── субтитры появились ──▶ cancelled (reason = subtitles)
любое ── индексатор подтвердил удаление ──▶ cancelled (reason = deleted)
published / no_speech ── смена source_version или reprocess ──▶ queued
```

- **Исходы FR-14.** Каждое состояние относится ровно к одному исходу:
  - `published` — распознана;
  - `queued`, `preparing`, `leased`, `transcribed` — ждёт распознавания;
  - `retry` — временный сбой;
  - `audio_unavailable` — звук недоступен;
  - `no_speech` — речь не найдена;
  - `permanent` — постоянный сбой;
  - `cancelled` / `subtitles` — заменена субтитрами;
  - `cancelled` / `deleted` — снята после подтверждённого удаления.
- **Итоговые для первичного прохода** (FR-19, SC-03): `published`, `audio_unavailable`, `no_speech`, `permanent`, `cancelled`.
- **Задание с `audio_unavailable`** перепроверяется не чаще раза в неделю, в начале запуска.
- **Причина `deleted` у `audio_unavailable`** означает отказ VK при получении звука, а не подтверждённое удаление. `asr-gateway` не снимает запись из поиска. Удаление подтверждает индексатор по правилам 003, и тогда задание получает `cancelled` / `deleted`.
- **Длинная запись.** Отдельного состояния нет. Запись любой длины обрабатывается целиком в пределах `1,5 × длительность + 15 мин`. Превышение предела — временный сбой `timeout`, после трёх запусков подряд — `permanent`.

### `asr_transcript` — расшифровка

| Поле | Тип | Правило |
| --- | --- | --- |
| `id` | `bigserial PK` | |
| `owner_id`, `item_id` | `bigint` | FK на `asr_job` |
| `source_version` | `text NOT NULL` | |
| `processor_version` | `text NOT NULL` | `asr-1/large-v3` |
| `language` | `text NOT NULL` | |
| `segments` | `jsonb NOT NULL` | Массив `{start_ms, end_ms, text, avg_logprob, no_speech_prob, compression_ratio}`, `0 ≤ start_ms < end_ms` |
| `audio_seconds`, `processing_seconds` | `numeric NOT NULL` | Для SC-09 |
| `version_id` | `bigint NULL FK material_version` | Заполняется после публикации |
| `created_at` | `timestamptz NOT NULL` | |
| **UNIQUE** | `(owner_id, item_id, source_version, processor_version)` | Повторный результат не создаёт второй расшифровки |

### `asr_run` — запуск распознавания

| Поле | Тип | Правило |
| --- | --- | --- |
| `id` | `bigserial PK` | |
| `worker_id` | `bigint FK asr_worker` | |
| `kind` | `text NOT NULL` | `initial`, `scheduled`, `catch_up`, `manual` |
| `only_refs` | `text[] NULL` | Записи внеочередного запуска по списку. `NULL` — вся очередь. Имя `only` — зарезервированное слово SQL |
| `started_at` | `timestamptz NOT NULL` | |
| `until` | `timestamptz NULL` | Конец окна. `NULL` у первичного прохода |
| `finished_at` | `timestamptz NULL` | |
| `outcome` | `text NULL` | `queue_empty`, `window_closed`, `paused`, `worker_lost`, `resource_unavailable`, `error` |
| `taken`, `published`, `failed` | `integer NOT NULL DEFAULT 0` | |
| `audio_seconds`, `processing_seconds` | `numeric NOT NULL DEFAULT 0` | |

**Одновременно не больше одного запуска:** частичный уникальный индекс по `finished_at IS NULL`. Запуск без связи с ПК дольше срока аренды закрывается с исходом `worker_lost`.

## Связи с индексатором (R7)

- **Запись без пригодной дорожки.** `indexer-daemon` при каждом проходе делает upsert в `asr_job`: создаёт задание, заполняет `source_version` у задания из миграции или возвращает задание в `queued`, если изменилась непустая `source_version`. Для этого `VideoSource` получает поля `duration` и `date` из `video.get`. Опубликованную распознанную версию он не заменяет карточкой, в том числе при изменившейся `source_version`: распознанная версия остаётся в поиске до публикации новой.
- **Подтверждённое удаление.** `unpublish_deleted` ставит заданию `cancelled` / `deleted`.
- **Появились субтитры.** Индексатор публикует версию `speech_source = 'subtitles'` и ставит заданию `cancelled` / `subtitles`, сбрасывая `lease_id`.
- **Миграция.** Создаёт `asr_job` с `initial = true` и `source_version = NULL` для всех `index_job` с `item_type = 'video' AND waiting_source`.

## Оповещения (таблица `alert` из 003)

| `cause_key` | Когда | Закрывается |
| --- | --- | --- |
| `asr_permanent:<owner>_<item>` | Задание перешло в `permanent` | `requeue` оператора |
| `asr_silent` | `max(last_seen_at)` старше `silence_alert_days` | Первый `session` после паузы |
