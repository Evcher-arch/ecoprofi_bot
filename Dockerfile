# Две цели сборки: app — bot и vk-adapter; indexer — индексатор и тесты
# (дополнительно чтение PDF/RTF и распознавание Tesseract с русской моделью).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PYTHONPATH=/app

WORKDIR /app
RUN useradd --create-home --uid 10001 ecoprofi

# Зависимости ставятся отдельным слоем: правка кода не пересобирает их.
COPY pyproject.toml ./
RUN mkdir ecoprofi && touch ecoprofi/__init__.py     && pip install --no-cache-dir ".[test]"     && pip uninstall -y ecoprofi     && rm -rf ecoprofi build *.egg-info


FROM base AS app

COPY ecoprofi ./ecoprofi
COPY migrations ./migrations
USER ecoprofi
CMD ["python", "-c", "print('Укажите команду сервиса в compose.yaml')"]


FROM base AS indexer

RUN apt-get update     && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus fonts-dejavu-core     && rm -rf /var/lib/apt/lists/*
RUN mkdir ecoprofi && touch ecoprofi/__init__.py     && pip install --no-cache-dir ".[test,indexer]"     && pip uninstall -y ecoprofi     && rm -rf ecoprofi build *.egg-info

COPY ecoprofi ./ecoprofi
COPY migrations ./migrations
COPY tests ./tests
# Точка подключения тома со значениями доступа оператора: только владелец.
RUN mkdir /secrets && chown ecoprofi:ecoprofi /secrets && chmod 700 /secrets
USER ecoprofi
CMD ["python", "-c", "print('Укажите команду сервиса в compose.yaml')"]


FROM node:24.14.1-bookworm-slim AS miniapp-build
WORKDIR /build
COPY mini-app/package.json mini-app/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY mini-app/ ./
RUN npm run build


FROM nginx:1.29.3-alpine AS miniapp-gateway
COPY deploy/miniapp-nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/miniapp-server.inc /etc/nginx/conf.d/miniapp-server.inc
COPY deploy/miniapp-realip.inc /etc/nginx/conf.d/miniapp-realip.inc
COPY deploy/miniapp-proxy.inc /etc/nginx/conf.d/miniapp-proxy.inc
COPY --from=miniapp-build /build/dist /usr/share/nginx/html
# Каталоги для монтирования TLS-блока и сертификата Cloudflare Origin CA.
# Пустой conf.d/tls означает «только локальный 8080» (см. deploy/miniapp-nginx.conf).
RUN mkdir -p /etc/nginx/conf.d/tls /etc/nginx/certs
EXPOSE 8080 443
