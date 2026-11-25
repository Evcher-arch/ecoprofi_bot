# miniapp-tunnel разделяет сетевой namespace шлюза и обращается по loopback.
# Только этому соединению доверяем CF-Connecting-IP от Cloudflare.
# Запросы через опубликованный Docker-порт и от соседних контейнеров не доверены.
set_real_ip_from 127.0.0.1;
set_real_ip_from ::1;
real_ip_header CF-Connecting-IP;
