# AdRobot · Keitaro — образ приложения.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
# Эмулятор Keitaro нужен демо-режиму (scripts/demo_server.py); остальные тесты в образ не идут.
COPY tests/__init__.py tests/fake_keitaro.py ./tests/

# Не root: приложению нужен только каталог с базой SQLite.
RUN useradd --create-home --uid 10001 adrobot \
    && mkdir -p /app/data \
    && chown -R adrobot:adrobot /app/data
USER adrobot

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/auth/mode', timeout=4).status == 200 else 1)"

# Миграции выполняются при каждом старте: они идемпотентны.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
