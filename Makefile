.PHONY: install lint typecheck test check corpus up demo chaos down

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest -q
	cd sandbox/shoplite && uv run --group dev pytest -q

check: lint typecheck test

corpus:
	uv run python -m rootline.corpus.render
	uv run python -m rootline.corpus.brownfield

up:
	docker compose up --build

# Adds the ShopLite sandbox, so approved SOP steps run against a live service. Runs in the
# background, so the same terminal can break something with `make chaos`.
demo:
	docker compose --profile demo up -d --build

# Breaks ShopLite on purpose, for example: make chaos FAULT=db_pool_exhausted
FAULT ?= db_pool_exhausted
chaos:
	curl -s -X POST localhost:8080/chaos/$(FAULT)

down:
	docker compose --profile demo down
