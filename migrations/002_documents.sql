-- Расписание, документы и фрагменты (specs/002-slides-letters-schedule/data-model.md).
-- Данные 001 сохраняются: записи и эпизоды переносятся на месте.

-- === Лекция: строка расписания ===
ALTER TABLE lecture
    ADD COLUMN lecture_date date,
    ADD COLUMN section      text NOT NULL DEFAULT '',
    -- planned — «в разработке» или без ссылки; orphaned — исчезла из расписания.
    ADD COLUMN state        text NOT NULL DEFAULT 'has_materials'
        CHECK (state IN ('planned', 'has_materials', 'orphaned')),
    ADD COLUMN missed       integer NOT NULL DEFAULT 0,
    ADD COLUMN last_seen_at timestamptz;

-- Публикации, на которые ссылается строка расписания. Пометка «Клуб» действует
-- на конкретный материал строки.
CREATE TABLE lecture_post (
    lecture_id    bigint  NOT NULL REFERENCES lecture (id) ON DELETE CASCADE,
    post_id       bigint  NOT NULL,
    row_role      text    NOT NULL CHECK (row_role IN ('lecture', 'review')),
    title         text    NOT NULL DEFAULT '',
    schedule_club boolean NOT NULL DEFAULT false,
    PRIMARY KEY (lecture_id, post_id)
);
CREATE INDEX lecture_post_post_idx ON lecture_post (post_id);

-- Выборка пилота: документы обрабатываются только для этих строк.
CREATE TABLE pilot_lecture (
    schedule_row text PRIMARY KEY
);

-- === Материал: запись, презентация, письмо, документ ===
ALTER TABLE material RENAME COLUMN video_id TO item_id;
ALTER TABLE material
    ADD COLUMN item_type     text NOT NULL DEFAULT 'video' CHECK (item_type IN ('video', 'doc')),
    ADD COLUMN kind          text NOT NULL DEFAULT 'video'
        CHECK (kind IN ('video', 'presentation', 'letter', 'document')),
    ADD COLUMN source_format text NOT NULL DEFAULT 'video',
    ADD COLUMN req_date      date,
    ADD COLUMN req_number    text,
    ADD COLUMN req_sender    text,
    -- confirmed — из текстового слоя оригинала или по решению редактора.
    ADD COLUMN req_state     text NOT NULL DEFAULT 'absent'
        CHECK (req_state IN ('confirmed', 'unverified', 'absent')),
    -- Поисковая подпись: номера документов из названия и подтверждённых реквизитов.
    ADD COLUMN doc_numbers   text[] NOT NULL DEFAULT '{}';

ALTER TABLE material DROP CONSTRAINT material_owner_id_video_id_key;
ALTER TABLE material ADD CONSTRAINT material_item_key UNIQUE (item_type, owner_id, item_id);

UPDATE material m SET doc_numbers = coalesce((
    SELECT array_agg(DISTINCT x[1])
    FROM regexp_matches(lower(m.title), '[0-9a-zа-яё]+(?:[-/][0-9a-zа-яё]+)+', 'g') AS x
    WHERE x[1] ~ '[0-9]'
), '{}');

-- Связь материала с лекцией и публикацией. Документ, приложенный к двум
-- публикациям, — один материал с двумя связями.
CREATE TABLE material_link (
    id          bigserial PRIMARY KEY,
    lecture_id  bigint  NOT NULL REFERENCES lecture (id) ON DELETE CASCADE,
    material_id bigint  NOT NULL REFERENCES material (id) ON DELETE CASCADE,
    post_id     bigint  NOT NULL,
    link_type   text    NOT NULL CHECK (link_type IN ('attachment', 'mention')),
    -- NULL — лента публикации ещё не проверена (связи, перенесённые из 001).
    paid_feed   boolean,
    UNIQUE (lecture_id, material_id, post_id)
);
CREATE INDEX material_link_material_idx ON material_link (material_id);

INSERT INTO material_link (lecture_id, material_id, post_id, link_type)
SELECT lecture_id, id, post_id, link_type FROM material;

ALTER TABLE material DROP COLUMN lecture_id, DROP COLUMN post_id, DROP COLUMN link_type;

-- === Решения редактора ===
ALTER TABLE material_policy RENAME COLUMN video_id TO item_id;
ALTER TABLE material_policy
    ADD COLUMN item_type   text NOT NULL DEFAULT 'video' CHECK (item_type IN ('video', 'doc')),
    -- Сигнал расписания, который видел редактор; NULL — решение принято до сигнала.
    ADD COLUMN seen_signal text CHECK (seen_signal IN ('public', 'club')),
    ADD COLUMN decided_at  timestamptz;
ALTER TABLE material_policy DROP CONSTRAINT material_policy_pkey;
ALTER TABLE material_policy ADD PRIMARY KEY (item_type, owner_id, item_id);

-- Реквизиты, подтверждённые редактором по оригиналу.
CREATE TABLE requisite_decision (
    item_type  text   NOT NULL CHECK (item_type IN ('video', 'doc')),
    owner_id   bigint NOT NULL,
    item_id    bigint NOT NULL,
    req_date   date,
    req_number text   NOT NULL,
    req_sender text,
    PRIMARY KEY (item_type, owner_id, item_id)
);

-- === Версия материала ===
ALTER TABLE material_version RENAME COLUMN track_state TO text_state;
ALTER TABLE material_version DROP CONSTRAINT material_version_track_state_check;
ALTER TABLE material_version
    ADD CONSTRAINT material_version_text_state_check CHECK (text_state IN ('ready', 'absent', 'partial')),
    ADD COLUMN pages_total      integer,
    ADD COLUMN pages_unreadable integer;

-- === Фрагмент: эпизод записи, страница презентации, часть письма ===
ALTER TABLE episode RENAME TO fragment;
ALTER SEQUENCE episode_id_seq RENAME TO fragment_id_seq;
ALTER INDEX episode_pkey RENAME TO fragment_pkey;
ALTER INDEX episode_version_idx RENAME TO fragment_version_idx;
ALTER INDEX episode_tsv_idx RENAME TO fragment_tsv_idx;
ALTER INDEX episode_embedding_idx RENAME TO fragment_embedding_idx;

DO $$
DECLARE c record;
BEGIN
    -- Прежнее ограничение интервала заменяется ограничением по виду фрагмента.
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'fragment'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) LIKE '%start_ms%'
    LOOP
        EXECUTE format('ALTER TABLE fragment DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;

ALTER TABLE fragment
    ALTER COLUMN start_ms DROP NOT NULL,
    ALTER COLUMN end_ms DROP NOT NULL,
    ADD COLUMN kind       text    NOT NULL DEFAULT 'episode' CHECK (kind IN ('episode', 'page', 'letter_part')),
    ADD COLUMN page_no    integer,
    ADD COLUMN part_no    integer,
    ADD COLUMN recognized boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT fragment_place_check CHECK (
        -- Явные IS NOT NULL: сравнение с NULL даёт NULL, и ограничение пропустило бы строку.
        (kind = 'episode' AND start_ms IS NOT NULL AND end_ms IS NOT NULL
            AND start_ms >= 0 AND start_ms < end_ms AND page_no IS NULL)
        OR (kind = 'page' AND page_no IS NOT NULL AND page_no >= 1 AND part_no IS NOT NULL AND part_no >= 1
            AND start_ms IS NULL AND end_ms IS NULL)
        OR (kind = 'letter_part' AND part_no IS NOT NULL AND part_no >= 1 AND page_no IS NULL
            AND start_ms IS NULL AND end_ms IS NULL)
    );

-- === Задание индексации ===
ALTER TABLE index_job RENAME COLUMN video_id TO item_id;
ALTER TABLE index_job
    ADD COLUMN item_type text NOT NULL DEFAULT 'video' CHECK (item_type IN ('video', 'doc'));
ALTER TABLE index_job DROP CONSTRAINT index_job_pkey;
ALTER TABLE index_job ADD PRIMARY KEY (item_type, owner_id, item_id);
ALTER TABLE index_job DROP CONSTRAINT index_job_failure_reason_check;
ALTER TABLE index_job ADD CONSTRAINT index_job_failure_reason_check
    CHECK (failure_reason IN ('timeout', 'empty', 'denied', 'parse', 'deleted', 'unsupported'));

-- === Права компонентов ===
GRANT SELECT ON lecture_post, material_link, pilot_lecture, requisite_decision TO ecoprofi_bot;

GRANT SELECT, INSERT, UPDATE, DELETE ON lecture_post, material_link TO ecoprofi_indexer;
-- Решения редактора индексатор только читает: меняет их импорт политики.
GRANT SELECT ON material_policy, pilot_lecture, requisite_decision TO ecoprofi_indexer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ecoprofi_indexer;
