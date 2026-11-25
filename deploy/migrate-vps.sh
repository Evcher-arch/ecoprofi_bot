#!/usr/bin/env bash
# Перенос рабочего контура ПК -> VPS, сторона VPS (specs/005-vps-migration, T028–T031, T033, T035).
# Запуск: cd /opt/ecoprofi && bash deploy/migrate-vps.sh <шаг>
# Шаги (строго по порядку):
#   stop-writers — остановить bot, vk-adapter, indexer-daemon на VPS (один рабочий контур, FR-07)
#   restore      — T028/T029: проверка SHA256, пересоздание БД ecoprofi, pg_restore, migrate, analyze
#   verify       — T030: сверка количеств с ПК; пустой diff — точка невозврата
#   start        — T031: bot, vk-adapter, miniapp-gateway (indexer-daemon — после входа оператора)
#   login        — T033: новый вход оператора (интерактивно, нужен терминал: ssh -t)
#   daemon       — T035: запуск indexer-daemon и indexer status
# Входные файлы в out/: ecoprofi-migration.dump, ecoprofi-migration.dump.sha256, counts-pc.txt
# (создаёт deploy/migrate-pc.ps1 -Step snapshot на ПК).
set -euo pipefail

cd "${PROJECT_DIR:-/opt/ecoprofi}"
DC=(docker compose -f compose.yaml -f compose.vps.yaml)
OUT=out
mkdir -p "$OUT" && chmod 700 "$OUT"

log() {
    local line
    line="$(TZ=Europe/Moscow date '+%F %T') МСК  $*"
    echo "$line"
    echo "$line" >> "$OUT/migration-vps.log"
}

die() { echo "ОСТАНОВКА: $*" >&2; exit 1; }

psql_owner() { "${DC[@]}" exec -T db psql -U ecoprofi_owner -v ON_ERROR_STOP=1 "$@"; }

running_services() { "${DC[@]}" --profile miniapp --profile daemon ps --status running --services; }

require_writers_stopped() {
    local w
    w=$(running_services | grep -xE 'bot|vk-adapter|indexer-daemon' || true)
    [ -z "$w" ] || die "на VPS работают писатели: $(echo "$w" | tr '\n' ' ')— сначала stop-writers"
}

# Количества строк по table-counts.sql; файл формируется внутри контейнера, как на ПК.
table_counts() {
    "${DC[@]}" cp deploy/table-counts.sql db:/tmp/counts.sql
    "${DC[@]}" exec -T db sh -c 'psql -U ecoprofi_owner -d ecoprofi -v ON_ERROR_STOP=1 -tA -f /tmp/counts.sql > /tmp/counts.txt'
    "${DC[@]}" cp db:/tmp/counts.txt "$1"
}

step="${1:-}"
case "$step" in
stop-writers)
    "${DC[@]}" --profile miniapp --profile daemon stop bot vk-adapter indexer-daemon
    log "stop-writers: bot, vk-adapter, indexer-daemon остановлены; db и miniapp-gateway работают"
    "${DC[@]}" --profile miniapp --profile daemon ps
    ;;

restore)
    require_writers_stopped
    [ -f "$OUT/ecoprofi-migration.dump" ] || die "нет $OUT/ecoprofi-migration.dump"
    [ -f "$OUT/ecoprofi-migration.dump.sha256" ] || die "нет $OUT/ecoprofi-migration.dump.sha256"
    (cd "$OUT" && sha256sum -c ecoprofi-migration.dump.sha256) || die "SHA256 дампа не совпадает с ПК"

    # Защита от затирания рабочих данных: БД VPS должна быть без данных (только schema_migrations).
    # Повторное восстановление поверх данных — только с FORCE_RESTORE=1 и только до шага start.
    has_schema=$(psql_owner -d ecoprofi -tAc "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename='schema_migrations'")
    if [ "$has_schema" != "0" ]; then
        table_counts "$OUT/counts-vps-before.txt"
        rows=$(grep -vE '^(migration|schema_migrations)\|' "$OUT/counts-vps-before.txt" | awk -F'|' '$2 ~ /^[0-9]+$/ {s += $2} END {print s + 0}')
        if [ "$rows" -gt 0 ] && [ "${FORCE_RESTORE:-0}" != "1" ]; then
            die "в БД VPS уже $rows строк данных (out/counts-vps-before.txt). Пересоздание только с FORCE_RESTORE=1"
        fi
    fi

    roles=$(psql_owner -d postgres -tAc "SELECT count(*) FROM pg_roles WHERE rolname IN ('ecoprofi_bot','ecoprofi_indexer')")
    [ "$roles" = "2" ] || die "нет ролей ecoprofi_bot/ecoprofi_indexer (их создаёт db/init или vps-deploy.py) — GRANT из дампа не применятся"

    # Роли живут на уровне кластера и сохраняются; пересоздаётся только БД.
    psql_owner -d postgres -c "DROP DATABASE IF EXISTS ecoprofi WITH (FORCE)"
    psql_owner -d postgres -c "CREATE DATABASE ecoprofi OWNER ecoprofi_owner"
    log "restore: БД ecoprofi пересоздана"

    "${DC[@]}" cp "$OUT/ecoprofi-migration.dump" db:/tmp/ecoprofi.dump
    "${DC[@]}" exec -T db pg_restore -U ecoprofi_owner -d ecoprofi --exit-on-error /tmp/ecoprofi.dump \
        || die "pg_restore упал — ПК остаётся рабочим контуром, разобрать причину (на ПК: migrate-pc.ps1 -Step resume)"
    "${DC[@]}" exec -T db rm -f /tmp/ecoprofi.dump
    log "restore: pg_restore выполнен"

    echo "--- migrate (ожидается: новых миграций нет) ---"
    "${DC[@]}" run --rm --no-deps bot python -m ecoprofi.bot.migrate
    # Статистика планировщика не переносится pg_restore (research R7).
    "${DC[@]}" exec -T db vacuumdb -U ecoprofi_owner -d ecoprofi --analyze-only
    log "restore: migrate и analyze выполнены"
    ;;

verify)
    [ -f "$OUT/counts-pc.txt" ] || die "нет $OUT/counts-pc.txt с ПК"
    table_counts "$OUT/counts-vps.txt"
    psql_owner -d ecoprofi -tA -c 'SELECT version()' \
        -c "SELECT 'vector ' || extversion FROM pg_extension WHERE extname='vector'" > "$OUT/versions-vps.txt"
    echo "--- версии VPS ---"; cat "$OUT/versions-vps.txt"
    if diff "$OUT/counts-pc.txt" "$OUT/counts-vps.txt"; then
        touch "$OUT/verify-ok"
        log "verify: количества совпадают с ПК ($(wc -l < "$OUT/counts-vps.txt") строк сверки) — SC-02 (количества)"
    else
        rm -f "$OUT/verify-ok"
        log "verify: РАСХОЖДЕНИЕ количеств"
        die "количества не совпадают — не продолжать, ПК остаётся рабочим контуром (migrate-pc.ps1 -Step resume)"
    fi
    ;;

start)
    [ -f "$OUT/verify-ok" ] || die "нет out/verify-ok — сначала verify с пустым diff"
    "${DC[@]}" --profile miniapp up -d bot vk-adapter miniapp-gateway
    log "start: bot, vk-adapter, miniapp-gateway запущены на перенесённых данных (cutover -> vps)"
    "${DC[@]}" --profile miniapp --profile daemon ps
    echo "Дальше на ПК: migrate-pc.ps1 -Step finalize (T032), затем здесь: login (T033) и daemon (T035)."
    ;;

login)
    [ -t 0 ] || die "нужен интерактивный терминал: ssh -t root@<IP> 'cd /opt/ecoprofi && bash deploy/migrate-vps.sh login'"
    "${DC[@]}" --profile indexer run --rm indexer python -m ecoprofi.indexer access login
    log "login: вход оператора выполнен"
    ;;

daemon)
    "${DC[@]}" --profile daemon up -d indexer-daemon
    "${DC[@]}" --profile indexer run --rm indexer python -m ecoprofi.indexer status
    log "daemon: indexer-daemon запущен"
    ;;

*)
    sed -n '2,13p' "$0"
    exit 2
    ;;
esac
