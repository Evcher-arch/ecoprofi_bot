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
USER ecoprofi
CMD ["python", "-c", "print('Укажите команду сервиса в compose.yaml')"]
