-- Сверка переноса (005, T023/T026/T041): точные количества строк всех таблиц public
-- и список применённых миграций. Запуск: psql -U ecoprofi_owner -d ecoprofi -tA -f table-counts.sql
SELECT table_name || '|' ||
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM public.%I', table_name), false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;

SELECT 'migration|' || name FROM schema_migrations ORDER BY name;
