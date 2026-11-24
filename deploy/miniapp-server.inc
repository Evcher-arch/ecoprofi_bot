# Общие настройки сервера и allowlist Mini App (004).
# Подключается из server-блока 8080: Cloudflare Tunnel и локальная диагностика.
# Служебные /api/search, /api/delivered, /api/alerts/*, /docs, /openapi.json недоступны.

root /usr/share/nginx/html;
client_max_body_size 32k;
server_tokens off;

add_header X-Content-Type-Options nosniff always;
add_header Referrer-Policy no-referrer always;
add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru" always;
# HSTS по HTTP игнорируется браузером, поэтому общий файл безопасен для 8080.
add_header Strict-Transport-Security "max-age=31536000" always;

error_page 429 = @rate_limited;
error_page 413 = @too_large;
location @rate_limited { default_type application/json; add_header Retry-After 1 always; add_header Cache-Control no-store always;
    return 429 '{"error":{"code":"rate_limited","message":"Слишком много запросов.","retryable":true}}'; }
location @too_large { default_type application/json; add_header Cache-Control no-store always;
    return 413 '{"error":{"code":"payload_too_large","message":"Слишком большой запрос.","retryable":false}}'; }

location = / {
    if ($request_method !~ ^(GET|HEAD)$) { return 405; }
    add_header Cache-Control no-store always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru" always;
    try_files /index.html =404;
}
location /assets/ {
    if ($request_method !~ ^(GET|HEAD)$) { return 405; }
    try_files $uri =404;
}

location = /api/miniapp/v1/session {
    if ($request_method !~ ^(POST|DELETE)$) { return 405; }
    limit_req zone=miniapp burst=20 nodelay;
    include /etc/nginx/conf.d/miniapp-proxy.inc;
}
location = /api/miniapp/v1/session/resume {
    if ($request_method != POST) { return 405; }
    limit_req zone=miniapp burst=20 nodelay;
    include /etc/nginx/conf.d/miniapp-proxy.inc;
}
location = /api/miniapp/v1/dialogs {
    if ($request_method != POST) { return 405; }
    limit_req zone=miniapp burst=20 nodelay;
    include /etc/nginx/conf.d/miniapp-proxy.inc;
}
location ~ "^/api/miniapp/v1/dialogs/[0-9a-fA-F-]{36}/requests$" {
    if ($request_method != POST) { return 405; }
    limit_req zone=miniapp burst=20 nodelay;
    include /etc/nginx/conf.d/miniapp-proxy.inc;
}
location ~ "^/api/miniapp/v1/dialogs/[0-9a-fA-F-]{36}/requests/[0-9a-fA-F-]{36}$" {
    if ($request_method != GET) { return 405; }
    limit_req zone=miniapp burst=20 nodelay;
    include /etc/nginx/conf.d/miniapp-proxy.inc;
}
location ~ "^/api/miniapp/v1/dialogs/[0-9a-fA-F-]{36}/history$" {
    if ($request_method != GET) { return 405; }
    limit_req zone=miniapp burst=20 nodelay;
    include /etc/nginx/conf.d/miniapp-proxy.inc;
}

# Неизвестные API — 404 JSON без HTML-fallback; прочие пути — 404.
location /api/ { default_type application/json; add_header Cache-Control no-store always;
    return 404 '{"error":{"code":"not_found","message":"Не найдено.","retryable":false}}'; }
location / { return 404; }
