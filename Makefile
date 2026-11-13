# Make targets for JobIndex S1+
# On Windows without make, use the .ps1 equivalents or run via: python -m <module>

.PHONY: up migrate test lint fmt clean

up:
	docker compose up -d

migrate:
	. .venv/bin/activate && alembic upgrade head

test:
	. .venv/bin/activate && python -m pytest -q

lint:
	. .venv/bin/activate && python -m ruff check src tests

fmt:
	. .venv/bin/activate && python -m ruff format src tests

clean:
	docker compose down -v
	rm -rf .venv .pytest_cache .ruff_cache .mypy_cache src/jobindex.egg-info