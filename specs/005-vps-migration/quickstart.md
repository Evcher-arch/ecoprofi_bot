# Справочник команд 005: стенд на VPS

**Дата:** 2026-11-24. Пошаговый порядок и проверки — [tasks.md](tasks.md). Здесь собраны команды, которые повторяются при эксплуатации.

На VPS все команды выполняются в `/opt/ecoprofi` под пользователем `deploy`. Чтобы не повторять оверлей:

```bash
echo 'alias dc="docker compose -f /opt/ecoprofi/compose.yaml -f /opt/ecoprofi/compose.vps.yaml --profile miniapp --profile daemon"' >> ~/.bashrc && . ~/.bashrc
```

## Файлы развёртывания

| Файл | Назначение |
| --- | --- |
| `compose.vps.yaml` | Оверлей: шлюз публикует 80/443, монтирует сертификаты и VPS-конфиг nginx |
| `deploy/vps/miniapp-nginx.conf` | 80 → ACME и 301; 443 → TLS Let's Encrypt и allowlist `miniapp-server.inc`; 8080 → диагностика на 127.0.0.1 |
| `deploy/backup.sh` | Дамп → проверка `pg_restore --list` → sha256 → S3 daily/weekly → ротация |
| `deploy/systemd/ecoprofi-backup.{service,timer}` | Запуск копии ежедневно в 03:30 МСК |
| `deploy/table-counts.sql` | Точные количества строк и список миграций для сверки |

`miniapp-realip.inc` и профиль `tunnel` на VPS не используются: шлюз видит адрес участника напрямую.

## Запуск и обновление

```bash
git pull --ff-only
dc build
dc run --rm --no-deps --entrypoint nginx miniapp-gateway -t
dc run --rm bot python -m ecoprofi.bot.migrate
dc up -d
dc ps
curl -sSI https://miniapp.bot-alimony.ru/ | head -20
```

Перед миграцией, меняющей схему, сначала выполнить внеплановую копию: `sudo systemctl start ecoprofi-backup.service`.

## Сертификат

Первичный выпуск выполняется при свободном порту 80, до запуска шлюза:

```bash
sudo certbot certonly --standalone -d miniapp.bot-alimony.ru -m <почта> --agree-tos --no-eff-email
sudo mkdir -p /var/www/certbot
```

Хук перезагрузки шлюза после продления:

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-miniapp.sh >/dev/null <<'EOF'
#!/bin/sh
docker compose -f /opt/ecoprofi/compose.yaml -f /opt/ecoprofi/compose.vps.yaml --profile miniapp exec -T miniapp-gateway nginx -s reload
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-miniapp.sh
```

После запуска шлюза (порт 80 занят nginx) продление переключается на webroot. `reconfigure` сам выполняет пробное продление через `/.well-known/acme-challenge/`:

```bash
sudo certbot reconfigure --cert-name miniapp.bot-alimony.ru --webroot -w /var/www/certbot
sudo certbot renew --dry-run
```

Если оставить `standalone`, продление сорвётся, потому что порт 80 занят шлюзом. Таймер `certbot.timer` пакета Ubuntu продлевает сертификат автоматически, состояние видно в `systemctl list-timers certbot`.

## Сверка количеств

Файл результата формируется внутри контейнера, поэтому кодировка PowerShell на ПК на него не влияет.

```powershell
# [ПК]
docker compose cp deploy/table-counts.sql db:/tmp/counts.sql
docker compose exec -T db sh -c "psql -U ecoprofi_owner -d ecoprofi -tA -f /tmp/counts.sql > /tmp/counts.txt"
docker compose cp db:/tmp/counts.txt out/counts-pc.txt
```

```bash
# [VPS]
docker compose exec -T db psql -U ecoprofi_owner -d ecoprofi -tA < deploy/table-counts.sql > out/counts-vps.txt
diff out/counts-pc.txt out/counts-vps.txt && echo "совпадает"
```

## Резервная копия

```bash
sudo tee /etc/ecoprofi-backup.env >/dev/null <<'EOF'
PROJECT_DIR=/opt/ecoprofi
RCLONE_REMOTE=ecoprofi-s3:<имя бакета>
LOCAL_KEEP=3
EOF
sudo chmod 644 /etc/ecoprofi-backup.env
rclone config                        # remote "ecoprofi-s3", тип s3, endpoint и ключ провайдера
chmod 600 ~/.config/rclone/rclone.conf
sudo cp deploy/systemd/ecoprofi-backup.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ecoprofi-backup.timer
sudo systemctl start ecoprofi-backup.service && journalctl -u ecoprofi-backup -n 20 --no-pager
rclone ls ecoprofi-s3:<имя бакета>/daily/
```

Пробное восстановление выполняется в изолированном контейнере без сети `internal`, bot и индексатора:

```bash
rclone copy ecoprofi-s3:<имя бакета>/daily/<файл>.dump out/restore/
docker run -d --name restore-check -e POSTGRES_PASSWORD=restore-only -e POSTGRES_DB=ecoprofi -e POSTGRES_USER=ecoprofi_owner pgvector/pgvector:pg16
sleep 5   # дождаться pg_isready
docker exec restore-check psql -U ecoprofi_owner -d ecoprofi -c "CREATE ROLE ecoprofi_bot; CREATE ROLE ecoprofi_indexer;"
time docker exec -i restore-check pg_restore -U ecoprofi_owner -d ecoprofi --exit-on-error < out/restore/<файл>.dump
docker exec -i restore-check psql -U ecoprofi_owner -d ecoprofi -tA < deploy/table-counts.sql > out/counts-restore.txt
docker compose exec -T db psql -U ecoprofi_owner -d ecoprofi -tA < deploy/table-counts.sql > out/counts-now.txt
diff out/counts-now.txt out/counts-restore.txt   # расхождения — только записи, появившиеся после копии
docker rm -f restore-check && rm -rf out/restore
```

## Диагностика

| Симптом | Проверка |
| --- | --- |
| Сайт не открывается снаружи | `dc ps`; `curl -sSI http://127.0.0.1:8080/`; `sudo ss -tlnp \| grep -E ':80\|:443'`; `sudo ufw status`; панель провайдера: внешний брандмауэр |
| Ошибка сертификата | `sudo certbot certificates`; `dc logs --tail 50 miniapp-gateway` |
| 502 на API | `dc logs --tail 50 bot`; `dc ps bot` |
| DNS указывает не туда | с ПК: `Resolve-DnsName miniapp.bot-alimony.ru -Server 8.8.8.8` и `-Server 77.88.8.8` |
| Нет свежей копии | `systemctl list-timers ecoprofi-backup`; `journalctl -u ecoprofi-backup -n 50` |
