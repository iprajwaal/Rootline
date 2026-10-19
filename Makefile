.PHONY: install lint typecheck test check corpus up down

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

corpus:
	uv run python -m rootline.corpus.render
	uv run python -m rootline.corpus.brownfield

up:
	docker compose up --build

down:
	docker compose down
