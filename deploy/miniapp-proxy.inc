# Общие параметры проксирования к bot; X-Forwarded-* выставляет только шлюз.
# Имя bot разрешается через встроенный DNS Docker (127.0.0.11) в момент запроса,
# а не при старте nginx: иначе шлюз падает с «host not found in upstream»,
# если контейнер bot ещё не зарегистрирован в сети или был пересоздан.
resolver 127.0.0.11 valid=10s ipv6=off;
set $miniapp_bot_upstream http://bot:8000;
proxy_pass $miniapp_bot_upstream;
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Proto https;
proxy_set_header X-Forwarded-For $remote_addr;
# $remote_addr уже восстановлен из CF-Connecting-IP (miniapp-realip.inc).
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header Authorization $http_authorization;
proxy_set_header Origin $http_origin;
proxy_set_header X-Service-Key "";
proxy_connect_timeout 5s;
proxy_read_timeout 30s;
proxy_intercept_errors off;
add_header Cache-Control no-store always;
