-- Роли уже могут существовать после выполнения 01_roles.sh.
\getenv bot_password BOT_DB_PASSWORD
\getenv indexer_password INDEXER_DB_PASSWORD
SELECT format('CREATE ROLE ecoprofi_bot LOGIN PASSWORD %L', :'bot_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecoprofi_bot') \gexec
SELECT format('CREATE ROLE ecoprofi_indexer LOGIN PASSWORD %L', :'indexer_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecoprofi_indexer') \gexec
CREATE EXTENSION IF NOT EXISTS vector;
