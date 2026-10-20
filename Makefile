.PHONY: install lint typecheck test check corpus up demo chaos act down eval experiments scenarios reset cluster cluster-down chaos-k8s reset-k8s

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

# Fixes ShopLite by hand, the way an engineer would without an SOP: make act ACTION=clear_tmp
ACTION ?= clear_tmp
act:
	curl -s -X POST localhost:8080/admin/actions/$(ACTION)

down:
	docker compose --profile demo down

# Runs the dev or the held-out test queries and writes eval/runs/<time>-<split>/report.md
SPLIT ?= dev
eval:
	uv run python -m rootline.eval.runner --split $(SPLIT)

# Search, intent and classification comparisons plus the threshold sweep, on dev only
experiments:
	uv run python -m rootline.eval.experiments

# Scenarios 1-8 and 1b against the running stack (make demo first)
scenarios:
	uv run python -m rootline.scenarios

# Makes ShopLite healthy again between demo runs; its open alerts resolve within 5 s
reset:
	curl -s -X POST localhost:8080/reset

# A real shop on local Kubernetes (kind): orders-api, orders-worker, Postgres, Redis, Redpanda
cluster:
	kind get clusters | grep -qx shop || kind create cluster --config cluster/kind.yaml
	docker build -t shop-orders:dev cluster/orders
	kind load docker-image shop-orders:dev --name shop
	kubectl --context kind-shop apply -f cluster/manifests
	kubectl --context kind-shop -n shop rollout restart deploy
	for name in statefulset/orders-db statefulset/cache statefulset/order-queue \
		deploy/orders-api deploy/orders-worker deploy/traffic deploy/alerter; do \
		kubectl --context kind-shop -n shop rollout status $$name --timeout=300s || exit 1; \
	done
	./cluster/kubeconfig.sh

cluster-down:
	kind delete cluster --name shop

# Real faults on the kind shop: db_pool_exhausted, disk_full, cache_down, queue_consumer_lag,
# bad_config_deploy
chaos-k8s:
	./cluster/chaos.sh $(FAULT)

reset-k8s:
	./cluster/reset.sh
