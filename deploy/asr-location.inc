# API распознавателя /asr/v1/ (specs/006-video-asr-transcriber, research R2).
# Подключается только в server-блок 443 файла deploy/vps/miniapp-nginx.conf.
# Зона limit_req asr объявлена там же на уровне http.
# Ключ распознавателя передаётся в Authorization как есть; журнал запросов не ведётся.
# Директива set не наследуется вложенными location, поэтому адрес задаётся в каждом.

location /asr/v1/ {
    limit_req zone=asr burst=10 nodelay;
    client_max_body_size 1m;
    access_log off;

    # Имя сервиса разрешается в момент запроса: шлюз стартует и без asr-gateway.
    resolver 127.0.0.11 valid=10s ipv6=off;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Authorization $http_authorization;
    proxy_connect_timeout 5s;
    proxy_read_timeout 120s;
    add_header Cache-Control no-store always;

    # Расшифровка лекции в gzip: до 16 МБ.
    location ~ ^/asr/v1/jobs/[^/]+/result$ {
        limit_req zone=asr burst=10 nodelay;
        client_max_body_size 16m;
        set $asr_upstream http://asr-gateway:8100;
        proxy_pass $asr_upstream;
    }
    # Звук отдаётся потоком; заголовок Range клиента проходит без изменений.
    location ~ ^/asr/v1/jobs/[^/]+/audio$ {
        limit_req zone=asr burst=10 nodelay;
        proxy_buffering off;
        proxy_read_timeout 300s;
        set $asr_upstream http://asr-gateway:8100;
        proxy_pass $asr_upstream;
    }

    set $asr_upstream http://asr-gateway:8100;
    proxy_pass $asr_upstream;
}
