#!/bin/sh
# Выполняется один раз при создании тома БД: раздельные роли компонентов.
# Права на таблицы выдаёт миграция migrations/001_pilot.sql.
set -eu

psql -v ON_ERROR_STOP=1 \
     -v bot_password="$BOT_DB_PASSWORD" \
     -v indexer_password="$INDEXER_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE ecoprofi_bot LOGIN PASSWORD :'bot_password';
CREATE ROLE ecoprofi_indexer LOGIN PASSWORD :'indexer_password';
CREATE EXTENSION IF NOT EXISTS vector;
SQL
