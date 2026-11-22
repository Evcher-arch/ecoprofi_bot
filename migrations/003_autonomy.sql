-- Автономный индексатор (specs/003-autonomous-indexer/data-model.md).
-- Каталог, версии и фрагменты не меняются. Значений доступа оператора в схеме нет:
-- они лежат файлом на томе индексатора.

-- === Проход ===
CREATE TABLE index_run (
    id               bigserial PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('full', 'scheduled', 'manual')),
    state            text NOT NULL CHECK (state IN ('running', 'interrupted', 'done', 'paused', 'stopped',
                                                    'failed', 'skipped', 'cancelled')),
    stop_reason      text CHECK (stop_reason IN ('auth', 'rate_limit', 'ai_limit', 'paid_feed', 'schedule', 'busy')),
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    portion_size     integer NOT NULL DEFAULT 5 CHECK (portion_size >= 1),
    -- Число записей платной ленты при последней проверке: доступ оператора к ленте
    -- зависит от его подписки или прав и не равен допуску участника (FR-23).
    paid_feed_count  integer,
    n_new            integer NOT NULL DEFAULT 0,
    n_changed        integer NOT NULL DEFAULT 0,
    n_unchanged      integer NOT NULL DEFAULT 0,
    n_deferred       integer NOT NULL DEFAULT 0,
    n_failed         integer NOT NULL DEFAULT 0,
    -- ok / failed / NULL (расписание лекций в этом проходе не читалось)
    schedule_outcome text CHECK (schedule_outcome IN ('ok', 'failed')),
    error            text
);
-- Незавершённый проход один.
CREATE UNIQUE INDEX index_run_open_idx ON index_run ((true))
    WHERE state IN ('running', 'interrupted', 'paused', 'stopped', 'failed');
CREATE INDEX index_run_started_idx ON index_run (started_at);

-- Список лекций прохода; порция — единица сохранения прогресса.
CREATE TABLE run_lecture (
    run_id     bigint  NOT NULL REFERENCES index_run (id) ON DELETE CASCADE,
    lecture_id bigint  NOT NULL REFERENCES lecture (id) ON DELETE CASCADE,
    position   integer NOT NULL,
    portion_no integer NOT NULL CHECK (portion_no >= 1),
    state      text    NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done')),
    -- NULL — все материалы лекции; иначе только названные ключи (плановое обновление).
    targets    text[],
    done_at    timestamptz,
    PRIMARY KEY (run_id, lecture_id)
);

-- === Задание индексации ===
ALTER TABLE index_job
    ADD COLUMN last_run_id    bigint,
    ADD COLUMN failed_runs    integer NOT NULL DEFAULT 0,
    ADD COLUMN waiting_source boolean NOT NULL DEFAULT false;
ALTER TABLE index_job DROP CONSTRAINT index_job_state_check;
ALTER TABLE index_job ADD CONSTRAINT index_job_state_check
    CHECK (state IN ('queued', 'running', 'done', 'retry', 'failed', 'permanent'));
ALTER TABLE index_job DROP CONSTRAINT index_job_failure_reason_check;
ALTER TABLE index_job ADD CONSTRAINT index_job_failure_reason_check
    CHECK (failure_reason IN ('timeout', 'empty', 'denied', 'parse', 'deleted', 'unsupported',
                              'auth', 'rate_limit', 'ai_limit'));

-- Число проходов подряд, в которых лента не вернула публикацию. Пропуск материал не снимает.
ALTER TABLE lecture_post ADD COLUMN missed integer NOT NULL DEFAULT 0;

-- === Состояние доступа оператора: без значений токенов ===
CREATE TABLE operator_access (
    id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
    state               text NOT NULL DEFAULT 'absent' CHECK (state IN ('absent', 'active', 'login_required')),
    logged_in_at        timestamptz,
    refreshed_at        timestamptz,
    access_expires_at   timestamptz,
    refresh_expires_at  timestamptz,
    -- Наблюдения, без которых автоматические запуски не включаются (FR-14).
    refresh_verified_at timestamptz,
    relogin_verified_at timestamptz,
    last_denial         text
);
INSERT INTO operator_access (state) VALUES ('absent');

-- === Расписание запусков: не чаще двух раз в сутки, утром и вечером ===
CREATE TABLE run_schedule (
    id          boolean PRIMARY KEY DEFAULT true CHECK (id),
    morning     time NOT NULL DEFAULT '07:00',
    evening     time DEFAULT '19:00',
    enabled     boolean NOT NULL DEFAULT false,
    last_due_at timestamptz,
    CHECK (evening IS NULL OR evening - morning >= interval '6 hours')
);
INSERT INTO run_schedule DEFAULT VALUES;

-- === Оповещение оператору ===
CREATE TABLE alert (
    id            bigserial PRIMARY KEY,
    cause_key     text NOT NULL,
    kind          text NOT NULL DEFAULT 'action' CHECK (kind IN ('action', 'info')),
    body          text NOT NULL,
    state         text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    opened_at     timestamptz NOT NULL DEFAULT now(),
    closed_at     timestamptz,
    last_sent_at  timestamptz,
    sent_count    integer NOT NULL DEFAULT 0,
    recovery_sent boolean NOT NULL DEFAULT false
);
-- Одна открытая запись на причину.
CREATE UNIQUE INDEX alert_open_idx ON alert (cause_key) WHERE state = 'open';

-- === Права компонентов ===
GRANT SELECT, INSERT, UPDATE, DELETE ON index_run, run_lecture, run_schedule, operator_access, alert
    TO ecoprofi_indexer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ecoprofi_indexer;

-- bot читает состояние для оповещений, сам создаёт причину «индекс устарел» и отмечает отправку.
GRANT SELECT ON index_run, run_lecture, run_schedule, operator_access, index_job TO ecoprofi_bot;
GRANT SELECT, INSERT, UPDATE ON alert TO ecoprofi_bot;
GRANT USAGE, SELECT ON SEQUENCE alert_id_seq TO ecoprofi_bot;
