-- Раздельные роли компонентов и расширение vector.
-- Права на таблицы выдаёт миграция 001_pilot.sql.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecoprofi_bot') THEN
        CREATE ROLE ecoprofi_bot LOGIN PASSWORD :'bot_password';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecoprofi_indexer') THEN
        CREATE ROLE ecoprofi_indexer LOGIN PASSWORD :'indexer_password';
    END IF;
END
$$;

CREATE EXTENSION IF NOT EXISTS vector;
