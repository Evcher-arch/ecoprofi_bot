# Две цели сборки: app — bot и vk-adapter; indexer — индексатор и тесты
# (дополнительно чтение PDF/RTF и распознавание Tesseract с русской моделью).
FROM python:3.12-alpine AS base

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PYTHONPATH=/app     PIP_INDEX_URL=https://pypi.org/simple/     PIP_DEFAULT_TIMEOUT=100     PIP_MAX_RETRIES=10     PIP_RETRY_ON_TIMEOUT=True

WORKDIR /app
RUN addgroup -g 10001 ecoprofi && adduser -u 10001 -G ecoprofi -D ecoprofi

# Зависимости ставятся отдельным слоем: правка кода не пересобирает их.
COPY pyproject.toml ./
RUN mkdir ecoprofi && touch ecoprofi/__init__.py     && pip install --no-cache-dir ".[test]"     && pip uninstall -y ecoprofi     && rm -rf ecoprofi build *.egg-info


FROM base AS app

COPY ecoprofi ./ecoprofi
COPY migrations ./migrations
USER ecoprofi
CMD ["python", "-c", "print('\u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u0443 \u0441\u0435\u0440\u0432\u0438\u0441\u0430 \u0432 compose.yaml')"]


# indexer требует tesseract, которого нет в Alpine main/community
FROM python:3.12-slim AS indexer-base

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PYTHONPATH=/app     PIP_INDEX_URL=https://pypi.org/simple/     PIP_DEFAULT_TIMEOUT=100     PIP_MAX_RETRIES=10     PIP_RETRY_ON_TIMEOUT=True

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus fonts-dejavu-core ffmpeg     && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 10001 ecoprofi && useradd -u 10001 -g ecoprofi -m -s /bin/bash ecoprofi

COPY pyproject.toml ./
RUN mkdir ecoprofi && touch ecoprofi/__init__.py     && pip install --no-cache-dir ".[test,indexer]"     && pip uninstall -y ecoprofi     && rm -rf ecoprofi build *.egg-info


FROM indexer-base AS indexer

COPY ecoprofi ./ecoprofi
COPY migrations ./migrations
COPY tests ./tests
# Точка подключения тома со значениями доступа оператора: только владелец.
RUN mkdir /secrets && chown ecoprofi:ecoprofi /secrets && chmod 700 /secrets
# Временный звук распознавания (006): том asr-tmp подключается только к asr-gateway.
RUN mkdir /tmp/asr && chown ecoprofi:ecoprofi /tmp/asr && chmod 700 /tmp/asr
USER ecoprofi
CMD ["python", "-c", "print('\u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u0443 \u0441\u0435\u0440\u0432\u0438\u0441\u0430 \u0432 compose.yaml')"]


FROM node:24.14.1-bookworm-slim@sha256:b506e7321f176aae77317f99d67a24b272c1f09f1d10f1761f2773447d8da26c AS miniapp-build
WORKDIR /build
COPY mini-app/package.json mini-app/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY mini-app/ ./
RUN npm run build


FROM nginx:1.29.3-alpine@sha256:b3c656d55d7ad751196f21b7fd2e8d4da9cb430e32f646adcf92441b72f82b14 AS miniapp-gateway
COPY deploy/miniapp-nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/miniapp-server.inc /etc/nginx/conf.d/miniapp-server.inc
COPY deploy/miniapp-realip.inc /etc/nginx/conf.d/miniapp-realip.inc
COPY deploy/miniapp-proxy.inc /etc/nginx/conf.d/miniapp-proxy.inc
COPY --from=miniapp-build /build/dist /usr/share/nginx/html
EXPOSE 8080
