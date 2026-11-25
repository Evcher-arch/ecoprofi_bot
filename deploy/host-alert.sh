#!/usr/bin/env bash
# Оповещения хоста VPS (specs/005-vps-migration/contracts/host-alerts.md).
# Пишет причины tls и backup в таблицу alert (миграция 003) по тем же правилам,
# что ecoprofi/indexer/alerts.py: одна открытая запись на причину, доставку
# выполняют bot и vk-adapter. Код приложения и схема БД не меняются.
#
#   host-alert.sh open  <tls|backup> <текст>
#   host-alert.sh close <tls|backup>
#
# Коды возврата: 0 — успех, 2 — неверные аргументы, иное — сбой Docker или БД.
set -euo pipefail

usage() {
    echo "usage: host-alert.sh open <tls|backup> <текст> | host-alert.sh close <tls|backup>" >&2
    exit 2
}

[ "$#" -ge 2 ] || usage
action="$1"
cause="$2"
case "$cause" in
    tls|backup) ;;
    *) echo "host-alert: неизвестная причина '$cause' (допустимы tls, backup)" >&2; exit 2 ;;
esac

PROJECT_DIR="${PROJECT_DIR:-/opt/ecoprofi}"
cd "$PROJECT_DIR"

# Значения передаются переменными psql (:'cause', :'body'), а не подстановкой в SQL.
run_sql() {
    docker compose -f compose.yaml exec -T db \
        psql -U ecoprofi_owner -d ecoprofi -v ON_ERROR_STOP=1 -q "$@"
}

case "$action" in
    open)
        [ "$#" -eq 3 ] && [ -n "$3" ] || usage
        run_sql -v cause="$cause" -v body="$3" <<'SQL'
INSERT INTO alert (cause_key, kind, body) VALUES (:'cause', 'action', :'body')
ON CONFLICT (cause_key) WHERE state = 'open' DO UPDATE SET body = excluded.body;
SQL
        ;;
    close)
        [ "$#" -eq 2 ] || usage
        # О восстановлении сообщается, только если о причине уже сообщали (как в 003).
        run_sql -v cause="$cause" <<'SQL'
UPDATE alert SET state = 'closed', closed_at = now(), recovery_sent = (sent_count = 0)
WHERE cause_key = :'cause' AND state = 'open';
SQL
        ;;
    *) usage ;;
esac
