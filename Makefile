# Короткие команды для разработки. На Windows используйте команды из README.
PY ?= .venv/bin/python

.PHONY: install migrate run test lint security e2e clean

install:            ## виртуальное окружение + зависимости для разработки
	python3 -m venv .venv
	$(PY) -m pip install -r requirements-dev.txt

migrate:            ## создать/обновить схему базы
	$(PY) -m alembic upgrade head

run: migrate        ## запустить приложение на http://127.0.0.1:8000
	$(PY) -m uvicorn app.main:app --reload

test:               ## автотесты на эмуляторе Keitaro (сеть не нужна)
	$(PY) -m pytest

lint:               ## стиль и типичные ошибки
	$(PY) -m ruff check .

security:           ## статический анализ кода и известные уязвимости зависимостей
	$(PY) -m bandit -q -c pyproject.toml -r app scripts
	$(PY) -m pip_audit -r requirements.txt

e2e:                ## живая проверка на настоящем Keitaro (приложение должно быть запущено)
	$(PY) scripts/live_e2e.py

clean:
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
