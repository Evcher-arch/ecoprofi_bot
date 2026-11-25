#!/usr/bin/env bash
# Ежедневная проверка срока сертификата Mini App (005, research R10).
# Запускается от root таймером ecoprofi-cert-check.timer: каталог live/ доступен только root.
# Меньше CERT_WARN_DAYS дней до истечения (или файл не читается) — открыть причину tls,
# иначе закрыть. Let's Encrypt писем об истечении больше не рассылает.
set -euo pipefail

DOMAIN="miniapp.bot-alimony.ru"
CERT="${CERT_FILE:-/etc/letsencrypt/live/$DOMAIN/fullchain.pem}"
CERT_WARN_DAYS="${CERT_WARN_DAYS:-14}"
PROJECT_DIR="${PROJECT_DIR:-/opt/ecoprofi}"
ALERT="$PROJECT_DIR/deploy/host-alert.sh"

case "$CERT_WARN_DAYS" in
    ''|*[!0-9]*) echo "cert-check: CERT_WARN_DAYS должно быть целым числом" >&2; exit 2 ;;
esac

if ! end_raw="$(openssl x509 -enddate -noout -in "$CERT" 2>/dev/null)"; then
    "$ALERT" open tls "Сертификат $DOMAIN не читается.
Выполнить: sudo certbot certificates; sudo certbot renew."
    echo "cert-check: сертификат не читается, причина tls открыта"
    exit 0
fi

if openssl x509 -checkend "$((CERT_WARN_DAYS * 86400))" -noout -in "$CERT" >/dev/null; then
    "$ALERT" close tls
    echo "cert-check: запас не меньше $CERT_WARN_DAYS дн., причина tls закрыта"
    exit 0
fi

end_epoch="$(date -d "${end_raw#notAfter=}" +%s)"
days_left="$(( (end_epoch - $(date +%s)) / 86400 ))"
end_msk="$(TZ=Europe/Moscow date -d "@$end_epoch" '+%Y-%m-%d %H:%M')"
if [ "$days_left" -lt 0 ]; then
    first="Сертификат $DOMAIN истёк ($end_msk МСК)."
else
    first="Сертификат $DOMAIN истекает через $days_left дн. ($end_msk МСК)."
fi
"$ALERT" open tls "$first
Выполнить: sudo certbot renew; sudo certbot certificates."
echo "cert-check: $first Причина tls открыта"
