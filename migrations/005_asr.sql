-- 006: распознавание записей без субтитров (specs/006-video-asr-transcriber/data-model.md).
-- Очередь и аренда заданий, график распознавания, расшифровки и запуски.
-- Пишут indexer-daemon и asr-gateway (роль ecoprofi_indexer); bot читает только speech_source.

-- === Происхождение текста опубликованной версии ===
ALTER TABLE material_version
    ADD COLUMN speech_source text CHECK (speech_source IN ('subtitles', 'asr'));

UPDATE material_version v SET speech_source = 'subtitles'
FROM material m
WHERE m.id = v.material_id AND m.item_type = 'video' AND v.text_state = 'ready';

-- === Распознаватель ===
CREATE TABLE asr_worker (
    id           bigserial PRIMARY KEY,
    name         text NOT NULL UNIQUE,
    token_hash   text NOT NULL UNIQUE,          -- sha256 ключа в hex; самого ключа нет
    created_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz,
    last_seen_at timestamptz,
    version      text,
    device       jsonb
);

-- === График распознавания (одна строка, независим от run_schedule индексатора) ===
CREATE TABLE asr_schedule (
    id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
    weekdays            smallint[] NOT NULL DEFAULT '{7}'
        CHECK (cardinality(weekdays) >= 1 AND weekdays <@ '{1,2,3,4,5,6,7}'::smallint[]),
    start_at            time NOT NULL DEFAULT '01:00',           -- время МСК
    window_length       interval NOT NULL DEFAULT '8 hours'
        CHECK (window_length >= interval '30 minutes' AND window_length <= interval '24 hours'),
    catch_up            boolean NOT NULL DEFAULT false,
    paused              boolean NOT NULL DEFAULT false,
    initial_pass        text NOT NULL DEFAULT 'pending' CHECK (initial_pass IN ('pending', 'running', 'done')),
    initial_started_at  timestamptz,
    initial_done_at     timestamptz,
    manual_requested_at timestamptz,
    manual_only         text[],                                  -- запуск по списку <owner>_<item>
    last_due_at         timestamptz,
    silence_alert_days  integer NOT NULL DEFAULT 14 CHECK (silence_alert_days >= 1)
);
INSERT INTO asr_schedule DEFAULT VALUES;

-- === Запуск распознавания ===
CREATE TABLE asr_run (
    id                 bigserial PRIMARY KEY,
    worker_id          bigint REFERENCES asr_worker (id),
    kind               text NOT NULL CHECK (kind IN ('initial', 'scheduled', 'catch_up', 'manual')),
    only_refs          text[],                                   -- NULL — вся очередь
    started_at         timestamptz NOT NULL DEFAULT now(),
    until              timestamptz,                              -- NULL у первичного прохода
    finished_at        timestamptz,
    outcome            text CHECK (outcome IN ('queue_empty', 'window_closed', 'paused', 'worker_lost',
                                               'resource_unavailable', 'error')),
    taken              integer NOT NULL DEFAULT 0,
    published          integer NOT NULL DEFAULT 0,
    failed             integer NOT NULL DEFAULT 0,
    audio_seconds      numeric NOT NULL DEFAULT 0,
    processing_seconds numeric NOT NULL DEFAULT 0
);
-- Одновременно не больше одного запуска.
CREATE UNIQUE INDEX asr_run_one_open ON asr_run ((true)) WHERE finished_at IS NULL;

-- === Задание распознавания ===
CREATE TABLE asr_job (
    owner_id         bigint NOT NULL,
    item_id          bigint NOT NULL,
    material_id      bigint NOT NULL REFERENCES material (id) ON DELETE CASCADE,
    -- NULL — версия источника ещё не определена (задания из миграции): её заполняет
    -- индексатор или подготовка звука из свежего video.get.
    source_version   text,
    state            text NOT NULL DEFAULT 'queued' CHECK (state IN (
        'queued', 'preparing', 'leased', 'transcribed', 'published', 'audio_unavailable', 'no_speech',
        'retry', 'permanent', 'cancelled')),
    reason           text CHECK (reason IN ('denied', 'no_file', 'deleted', 'timeout', 'decode',
                                            'gpu_unavailable', 'server', 'subtitles', 'no_speech')),
    initial          boolean NOT NULL DEFAULT false,
    attempts         integer NOT NULL DEFAULT 0,
    failed_runs      integer NOT NULL DEFAULT 0,
    retry_at         timestamptz,
    lease_id         uuid,
    lease_worker_id  bigint REFERENCES asr_worker (id),
    lease_expires_at timestamptz,
    deadline_at      timestamptz,                                -- предел на запись после подготовки звука
    run_id           bigint REFERENCES asr_run (id),
    audio_path       text,
    audio_bytes      bigint,
    audio_sha256     text,
    duration_s       integer,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, item_id)
);
CREATE UNIQUE INDEX asr_job_lease ON asr_job (lease_id) WHERE lease_id IS NOT NULL;
CREATE INDEX asr_job_state ON asr_job (state, retry_at);

-- === Расшифровка ===
CREATE TABLE asr_transcript (
    id                 bigserial PRIMARY KEY,
    owner_id           bigint NOT NULL,
    item_id            bigint NOT NULL,
    source_version     text NOT NULL,
    processor_version  text NOT NULL,
    language           text NOT NULL,
    segments           jsonb NOT NULL,
    audio_seconds      numeric NOT NULL,
    processing_seconds numeric NOT NULL,
    version_id         bigint REFERENCES material_version (id) ON DELETE SET NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, item_id) REFERENCES asr_job (owner_id, item_id) ON DELETE CASCADE,
    UNIQUE (owner_id, item_id, source_version, processor_version)
);

-- === Начальная очередь: записи, ждущие источника на момент запуска функции ===
INSERT INTO asr_job (owner_id, item_id, material_id, initial, state)
SELECT j.owner_id, j.item_id, m.id, true, 'queued'
FROM index_job j
JOIN material m ON m.item_type = 'video' AND m.owner_id = j.owner_id AND m.item_id = j.item_id
WHERE j.item_type = 'video' AND j.waiting_source AND m.state <> 'deleted';

GRANT SELECT, INSERT, UPDATE, DELETE ON asr_worker, asr_schedule, asr_run, asr_job, asr_transcript
    TO ecoprofi_indexer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ecoprofi_indexer;
