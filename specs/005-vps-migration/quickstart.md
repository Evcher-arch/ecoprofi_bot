# Справочник команд 005: стенд на VPS

**Дата:** 2026-11-24. Пошаговый порядок и проверки — [tasks.md](tasks.md). Здесь собраны команды, которые повторяются при эксплуатации.

На VPS все команды выполняются в `/opt/ecoprofi` под пользователем `deploy`. Чтобы не повторять оверлей:

```bash
echo 'alias dc="docker compose -f /opt/ecoprofi/compose.yaml -f /opt/ecoprofi/compose.vps.yaml --profile miniapp --profile daemon"' >> ~/.bashrc && . ~/.bashrc
```

## Сквозная проверка функции

Функция считается работающей, когда каждый сценарий ниже имеет запись в [acceptance.md](acceptance.md). Подробные шаги приведены в tasks.md, внешняя поверхность описана в [contracts/vps-host.md](contracts/vps-host.md).

| Сценарий | Предусловие | Проверка | Ожидание | Критерий |
| --- | --- | --- | --- | --- |
| Вход из сетей | DNS → IP VPS, сертификат выпущен | `curl.exe -sSI https://miniapp.bot-alimony.ru/` с ПК и открытие с телефона в 3 мобильных сетях и 1 домашней | 200, CSP `frame-ancestors` VK, HSTS, TTFB ≤ 1 с | SC-01 |
| Данные без потерь | Восстановлен снимок переноса | раздел «Сверка количеств» и 4 контрольных вопроса | пустой `diff`, те же источники | SC-02 |
| Mini App с сервера | Сервисы VPS запущены | матрица 004 на телефоне и ПК (T038–T040) | как на локальном стенде | SC-03 |
| Перезагрузка | Стенд работает | `sudo reboot`, через 3 мин `dc ps` и `curl` | все Up/healthy, 200 | SC-04 |
| Восстановление копии | Есть ночная копия в S3 | раздел «Резервная копия», пробное восстановление | совпадение количеств, записаны длительность и давность копии | SC-05 |
| Автономность | ПК выключен | журнал проходов 07:00/19:00 за 7 суток | исход на каждый срок, ≤ 2 автоопросов в сутки | SC-06 |
| Сертификат | Шлюз на 80/443 | `sudo certbot renew --dry-run` | успешно | SC-07 |
| Поверхность и секреты | Стенд работает | `Test-NetConnection <IP> -Port 5432/8000/8080/22/80/443`, `dc logs \| grep -Ei "sign=\|token\|secret\|bearer\|password"` | открыты только 22/80/443, реальных значений в логах нет | SC-08 |
| Оповещения хоста | Юниты `cert-check` и `backup` установлены | раздел «Оповещения хоста» | сообщения об открытии и восстановлении дошли оператору | граничный случай «сертификат», FR-09 |

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
# статистика планировщика не переносится pg_restore (research R7)
docker compose exec -T db vacuumdb -U ecoprofi_owner -d ecoprofi --analyze-only
# версии для протокола и будущего возврата на ПК
docker compose exec -T db psql -U ecoprofi_owner -d ecoprofi -tAc "SELECT version(); SELECT extversion FROM pg_extension WHERE extname='vector';"
```

## Журналы Docker и зеркало реестра

`/etc/docker/daemon.json` настраивается до первого `up` (research R6). Ключ `registry-mirrors` добавляется, только если без него не работает T015:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "5" } }
EOF
sudo systemctl restart docker
```

## Оповещения хоста (план, файлы появятся по tasks.md)

Контракт описан в [contracts/host-alerts.md](contracts/host-alerts.md). Ниже приведены проверочные команды после установки юнитов:

```bash
deploy/host-alert.sh open tls "Проверка канала оповещений хоста"   # сообщение оператору в течение минуты
deploy/host-alert.sh close tls                                      # одно сообщение «Восстановлено»
CERT_WARN_DAYS=90 deploy/cert-check.sh                              # открывает tls при запасе < 90 дней
deploy/cert-check.sh                                                # закрывает tls при запасе ≥ 14 дней
systemctl list-timers ecoprofi-cert-check ecoprofi-backup
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
| DNS отвечает SERVFAIL после смены NS | `Resolve-DnsName bot-alimony.ru -Type DS -Server 8.8.8.8`: DS от Cloudflare не удалён у REG.RU (research R5) |
| Compose не загружает проект: нет файла секрета туннеля | `install -m 600 /dev/null deploy/secrets/cloudflared-token.txt` (research R12) |
