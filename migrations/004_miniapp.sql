-- Добавочные таблицы Mini App (004): никаких FK к participant/test_group/каталогу.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE miniapp_session (
    id uuid PRIMARY KEY,
    token_hash bytea NOT NULL UNIQUE,
    app_id bigint NOT NULL CHECK (app_id > 0),
    user_id bigint NOT NULL CHECK (user_id > 0),
    launch_digest bytea NOT NULL,
    launch_ts timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at),
    revoked_at timestamptz,
    current_dialog_id uuid
);
CREATE INDEX miniapp_session_token_hash_idx ON miniapp_session (token_hash);
CREATE INDEX miniapp_session_expires_idx ON miniapp_session (expires_at);

CREATE TABLE miniapp_dialog (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES miniapp_session (id) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'closed')),
    created_at timestamptz NOT NULL,
    closed_at timestamptz CHECK ((state = 'closed') = (closed_at IS NOT NULL)),
    next_sequence bigint NOT NULL DEFAULT 1 CHECK (next_sequence > 0),
    pending_query text,
    pending_request_id uuid,
    UNIQUE (session_id, id),
    UNIQUE (session_id, id, pending_request_id) DEFERRABLE INITIALLY DEFERRED,
    CHECK ((pending_query IS NULL) = (pending_request_id IS NULL))
);
CREATE UNIQUE INDEX miniapp_dialog_active_idx ON miniapp_dialog (session_id) WHERE state = 'active';

ALTER TABLE miniapp_session
    ADD CONSTRAINT miniapp_session_current_dialog_fk
    FOREIGN KEY (id, current_dialog_id) REFERENCES miniapp_dialog (session_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE miniapp_request (
    dialog_id uuid NOT NULL REFERENCES miniapp_dialog (id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    original_text text NOT NULL CHECK (char_length(original_text) BETWEEN 1 AND 2000 AND btrim(original_text) <> ''),
    text_digest bytea NOT NULL,
    effective_query text CHECK (effective_query IS NULL OR char_length(effective_query) <= 2000),
    context_request_id uuid,
    state text NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'retryable_failed', 'denied', 'invalidated', 'cancelled')),
    outcome text CHECK (outcome IS NULL OR outcome IN ('found', 'clarify', 'empty')),
    result jsonb,
    provenance jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(provenance) = 'array'),
    operation_id text NOT NULL,
    attempt_id uuid,
    attempt_no integer NOT NULL DEFAULT 0 CHECK (attempt_no BETWEEN 0 AND 3),
    error_code text,
    queued_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (dialog_id, request_id),
    UNIQUE (dialog_id, sequence),
    UNIQUE (dialog_id, request_id, context_request_id) DEFERRABLE INITIALLY DEFERRED,
    CHECK ((outcome IS NULL AND state <> 'completed') OR
           (outcome IS NOT NULL AND state = 'completed')),
    CHECK (state = 'completed' OR result IS NULL),
    CHECK (state NOT IN ('invalidated', 'cancelled') OR provenance = '[]'::jsonb)
);
CREATE INDEX miniapp_request_queue_idx ON miniapp_request (state, queued_at);
CREATE UNIQUE INDEX miniapp_request_active_idx ON miniapp_request (dialog_id) WHERE state IN ('queued', 'running');

ALTER TABLE miniapp_dialog
    ADD CONSTRAINT miniapp_dialog_pending_fk FOREIGN KEY (id, pending_request_id)
    REFERENCES miniapp_request (dialog_id, request_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE miniapp_request
    ADD CONSTRAINT miniapp_request_context_fk FOREIGN KEY (dialog_id, context_request_id)
    REFERENCES miniapp_request (dialog_id, request_id) DEFERRABLE INITIALLY DEFERRED;

GRANT SELECT, INSERT, UPDATE, DELETE ON miniapp_session, miniapp_dialog, miniapp_request TO ecoprofi_bot;
REVOKE ALL ON miniapp_session, miniapp_dialog, miniapp_request FROM ecoprofi_indexer;
