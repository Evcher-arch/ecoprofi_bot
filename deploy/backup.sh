#!/usr/bin/env bash
# Ночная резервная копия БД на VPS (005, T040). Запускается ecoprofi-backup.timer.
# Настройки — /etc/ecoprofi-backup.env:
#   PROJECT_DIR=/opt/ecoprofi
#   RCLONE_REMOTE=ecoprofi-s3:<имя бакета>   # remote из `rclone config` пользователя deploy
#   LOCAL_KEEP=3                             # сколько последних копий держать на диске VPS
# Хранение вне VPS: daily/ — 7 дней, weekly/ (воскресенье) — 4 недели.
# Ключи S3 лежат только в конфиге rclone (права 600), не в этом файле и не в Git.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/ecoprofi}"
RCLONE_REMOTE="${RCLONE_REMOTE:?задайте RCLONE_REMOTE в /etc/ecoprofi-backup.env}"
LOCAL_KEEP="${LOCAL_KEEP:-3}"
BACKUP_DIR="$PROJECT_DIR/out/backups"

stamp="$(date +%Y-%m-%d_%H%M)"
file="$BACKUP_DIR/ecoprofi-$stamp.dump"
umask 077
mkdir -p "$BACKUP_DIR"

cd "$PROJECT_DIR"
# -T: без TTY, иначе двоичный поток повреждается.
docker compose exec -T db pg_dump -U ecoprofi_owner -d ecoprofi -Fc > "$file.part"
# Пустой или усечённый дамп не выдаём за копию: pg_restore должен прочитать оглавление.
docker compose exec -T db pg_restore --list < "$file.part" > /dev/null
mv "$file.part" "$file"
sha256sum "$file" | sed "s#  .*/#  #" > "$file.sha256"

rclone copy --no-traverse "$file" "$RCLONE_REMOTE/daily/"
rclone copy --no-traverse "$file.sha256" "$RCLONE_REMOTE/daily/"
if [ "$(date +%u)" = "7" ]; then
    rclone copy --no-traverse "$file" "$RCLONE_REMOTE/weekly/"
    rclone copy --no-traverse "$file.sha256" "$RCLONE_REMOTE/weekly/"
fi

rclone delete --min-age 8d "$RCLONE_REMOTE/daily/"
rclone delete --min-age 29d "$RCLONE_REMOTE/weekly/"

# Локально — только последние LOCAL_KEEP копий.
ls -1t "$BACKUP_DIR"/ecoprofi-*.dump | tail -n +"$((LOCAL_KEEP + 1))" | while read -r old; do
    rm -f -- "$old" "$old.sha256"
done

echo "backup ok: $(basename "$file") $(stat -c %s "$file") bytes"
