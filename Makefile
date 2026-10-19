.PHONY: install lint typecheck test check up down

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest -q

check: lint typecheck test

up:
	docker compose up --build

down:
	docker compose down
