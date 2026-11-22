# Общий образ компонентов bot, indexer, vk-adapter и тестов.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Зависимости ставятся отдельным слоем: правка кода не пересобирает их.
COPY pyproject.toml ./
RUN mkdir ecoprofi && touch ecoprofi/__init__.py \
    && pip install --no-cache-dir ".[test]" \
    && pip uninstall -y ecoprofi \
    && rm -rf ecoprofi build *.egg-info

COPY ecoprofi ./ecoprofi
COPY migrations ./migrations
COPY tests ./tests

RUN useradd --create-home --uid 10001 ecoprofi
USER ecoprofi

CMD ["python", "-c", "print('Укажите команду сервиса в compose.yaml')"]
