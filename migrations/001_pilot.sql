-- Схема первого серверного сценария (specs/001-find-lecture-episode/data-model.md).
-- Применяется один раз; учёт — в таблице schema_migrations (ecoprofi/bot/migrate.py).

CREATE EXTENSION IF NOT EXISTS vector;

-- Лекция связана со строкой расписания.
CREATE TABLE lecture (
    id           bigserial PRIMARY KEY,
    schedule_row text NOT NULL UNIQUE,
    title        text NOT NULL
);

-- Материал связан с лекцией, постом и парой owner_id/video_id.
CREATE TABLE material (
    id                   bigserial PRIMARY KEY,
    lecture_id           bigint NOT NULL REFERENCES lecture (id),
    post_id              bigint NOT NULL,
    owner_id             bigint NOT NULL,
    video_id             bigint NOT NULL,
    link_type            text   NOT NULL CHECK (link_type IN ('attachment', 'mention')),
    title                text   NOT NULL,
    description          text   NOT NULL DEFAULT '',
    url                  text   NOT NULL,
    -- active — в поиске; review — на проверке; deleted — подтверждённое удаление.
    state                text   NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'review', 'deleted')),
    published_version_id bigint,
    UNIQUE (owner_id, video_id)
);

-- Аудитория материала задаётся импортом политики и может появиться раньше материала.
-- Отсутствие строки или значение unknown закрывает материал для выдачи.
CREATE TABLE material_policy (
    owner_id bigint NOT NULL,
    video_id bigint NOT NULL,
    audience text   NOT NULL CHECK (audience IN ('public', 'club', 'unknown')),
    PRIMARY KEY (owner_id, video_id)
);

CREATE TABLE material_version (
    id                bigserial PRIMARY KEY,
    material_id       bigint NOT NULL REFERENCES material (id) ON DELETE CASCADE,
    content_hash      text   NOT NULL,
    processor_version text   NOT NULL,
    embedding_model   text   NOT NULL,
    card_state        text   NOT NULL CHECK (card_state IN ('ready', 'missing')),
    track_state       text   NOT NULL CHECK (track_state IN ('ready', 'absent')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- Уникальность сочетания ключей; повтор не создаёт дублей.
    UNIQUE (material_id, content_hash, processor_version, embedding_model)
);

ALTER TABLE material
    ADD CONSTRAINT material_published_version_fk
    FOREIGN KEY (published_version_id) REFERENCES material_version (id) ON DELETE SET NULL;

CREATE TABLE episode (
    id         bigserial PRIMARY KEY,
    version_id bigint  NOT NULL REFERENCES material_version (id) ON DELETE CASCADE,
    start_ms   integer NOT NULL,
    end_ms     integer NOT NULL,
    text       text    NOT NULL,
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED,
    embedding  vector(256) NOT NULL,
    -- 0 ≤ начало < конец
    CHECK (start_ms >= 0 AND start_ms < end_ms)
);

CREATE INDEX episode_version_idx ON episode (version_id);
CREATE INDEX episode_tsv_idx ON episode USING gin (tsv);
CREATE INDEX episode_embedding_idx ON episode USING hnsw (embedding vector_cosine_ops);

-- Допуск и право участника.
CREATE TABLE test_group (
    group_id bigint PRIMARY KEY
);

CREATE TABLE participant (
    group_id bigint  NOT NULL REFERENCES test_group (group_id) ON DELETE CASCADE,
    vk_id    bigint  NOT NULL CHECK (vk_id > 0),
    tester   boolean NOT NULL DEFAULT false,
    role     text    NOT NULL DEFAULT 'unknown' CHECK (role IN ('public', 'club', 'unknown')),
    PRIMARY KEY (group_id, vk_id)
);

-- Контекст уточнения изолирован по группе, peer_id и user_id.
CREATE TABLE dialog_context (
    group_id      bigint NOT NULL,
    peer_id       bigint NOT NULL,
    user_id       bigint NOT NULL,
    pending_query text   NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, peer_id, user_id)
);

-- Событие обрабатывается однократно по составному ключу группы и события.
CREATE TABLE event (
    group_id        bigint NOT NULL,
    event_id        text   NOT NULL,
    peer_id         bigint NOT NULL,
    user_id         bigint NOT NULL,
    effective_query text   NOT NULL,
    random_id       bigint NOT NULL,
    state           text   NOT NULL DEFAULT 'answered' CHECK (state IN ('answered', 'delivered')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, event_id)
);

-- Задание индексации одного материала.
CREATE TABLE index_job (
    owner_id       bigint  NOT NULL,
    video_id       bigint  NOT NULL,
    post_id        bigint  NOT NULL,
    state          text    NOT NULL CHECK (state IN ('queued', 'running', 'done', 'retry', 'failed')),
    attempt        integer NOT NULL DEFAULT 0,
    failure_reason text    CHECK (failure_reason IN ('timeout', 'empty', 'denied', 'parse', 'deleted')),
    retry_at       timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, video_id)
);

-- Измерения по идентификатору операции. Секреты и подписанные URL сюда не пишутся.
CREATE TABLE operation_log (
    operation_id text NOT NULL,
    component    text NOT NULL CHECK (component IN ('bot', 'indexer')),
    outcome      text NOT NULL,
    details      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX operation_log_id_idx ON operation_log (operation_id);

-- Раздельные права компонентов. Роли создаёт db/init/01_roles.sh.
GRANT USAGE ON SCHEMA public TO ecoprofi_bot, ecoprofi_indexer;

GRANT SELECT ON lecture, material, material_policy, material_version, episode,
                test_group, participant TO ecoprofi_bot;
GRANT SELECT, INSERT, UPDATE, DELETE ON dialog_context, event TO ecoprofi_bot;
GRANT INSERT ON operation_log TO ecoprofi_bot;

GRANT SELECT, INSERT, UPDATE, DELETE ON lecture, material, material_version, episode,
                                        index_job TO ecoprofi_indexer;
GRANT INSERT ON operation_log TO ecoprofi_indexer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ecoprofi_indexer;
