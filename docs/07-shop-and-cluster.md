# Shop and cluster

Steps act on a `Sandbox`: either `ShopLite` (simulated) or `KubeShop` (a real shop on kind).
`SHOP_MODE=shoplite|k8s` picks one.

## Catalog

[`corpus/catalog.yaml`](../corpus/catalog.yaml) is shared by ShopLite, the cluster, ingestion and the agent:

- `services`: names, aliases, dependencies;
- `faults`: service, alert, error line, the check that shows it, the action that fixes it, and
  which docs cover it;
- `checks`: read-only commands;
- `actions`: commands that change things, run only after approval.

SOP steps are linked to catalog checks and actions at ingest. The executor refuses any name that
isn't in the catalog.

## Faults

| Fault | Alert | Fix | Docs | kind |
|---|---|---|---|---|
| `db_pool_exhausted` | OrdersDbPoolExhausted | `raise_pool_limit` | RCA-2025-014, SOP-DB-001 | yes |
| `queue_consumer_lag` | OrderQueueConsumerLagHigh | `scale_consumers` | RCA-2025-022, SOP-QUEUE-001/002 | yes |
| `cache_down` | OrdersApiHighLatency | `restart_cache` | RCA-2025-031, SOP-CACHE-001 | yes |
| `bad_config_deploy` | OrdersApiCrashLooping | `rollback_config` | RCA-2026-007, SOP-DEPLOY-002 (supersedes 001) | yes |
| `disk_full` | OrdersApiDiskFull | `clear_tmp` | none | yes |
| `db_read_only` | OrdersDbWritesFailing | `allow_db_writes` | none | yes |
| `payment_key_expired` | PaymentAuthFailures | `rotate_payment_key` | brownfield wiki export | no |
| `worker_oom` | OrdersWorkerOOMKilled | `restart_worker` | brownfield, mixed file split in two | no |

ShopLite has all eight faults.

## ShopLite

[`sandbox/shoplite/`](../sandbox/shoplite), a separate project:
- `POST /chaos/{fault}` and `POST /reset`;
- `GET /health`, `GET /admin/checks/{name}`;
- `POST /admin/actions/{name}`, and `POST /admin/actions/{name}/dry-run`, which applies the action
  to a copy of the state and returns the resulting health.

A watchdog posts Alertmanager webhooks to `/incidents` when a fault starts or clears.

## kind cluster

[`cluster/`](../cluster): namespace `shop`, API on port 16443, orders-api on NodePort 30080.

- `orders-api`: FastAPI. Orders go to Postgres, cache to Redis, events to Redpanda. Receipts are
  written to a 64Mi emptyDir at `/var/lib/shoplite`. `/ready` checks only the process; `/health`
  checks db, db writes, cache, queue and disk.
- `orders-worker`: consumer on topic `orders` (6 partitions).
- `traffic`: places orders in a loop.
- `reports`: the slow sales report from RCA-2025-014. 0 replicas unless the pool fault is on.
- `orders-db` (Postgres), `cache` (Redis, `podManagementPolicy: Parallel`), `order-queue`
  (Redpanda).
- `alerter`: probes every 5 s, fires after 2 failing probes, and posts to
  `host.docker.internal:8000/incidents` with the latest matching error line from orders-api logs.
  Crash loop means restartCount ≥ 2 and not ready. Lag is read from the worker group's committed
  offsets, threshold 300.

[`cluster/chaos.sh`](../cluster/chaos.sh) injects each fault for real:

| Fault | What it does |
|---|---|
| `db_pool_exhausted` | pool max 4, plus 6 slow report pods |
| `disk_full` | fills the receipts volume with junk files dated 2 days back |
| `cache_down` | moves Redis to port 6380 and kills all clients |
| `queue_consumer_lag` | 4 traffic pods at 0.1 s per order, against one worker |
| `bad_config_deploy` | blanks `PAYMENT_GATEWAY_URL`, so orders-api crash-loops |
| `db_read_only` | `alter system set default_transaction_read_only = on` |

[`cluster/reset.sh`](../cluster/reset.sh) undoes all of them and waits for `/health` to be ok.

### Accounts

[`cluster/kubeconfig.sh`](../cluster/kubeconfig.sh) writes `cluster/.kube/config` (gitignored) with two contexts:

- `reader`: get/list/watch pods, logs, events and workloads, plus exec. Used for checks and
  previews.
- `fixer`: the same, plus patch/update on deployments, statefulsets and their scale. Used for
  server-side dry runs and approved fixes.

### How KubeShop runs fixes

Catalog commands are written as you'd type them inside the cluster. `IN_CLUSTER` in
[`agent/kube.py`](../src/rootline/agent/kube.py) maps them to `kubectl exec ...`.

| Fix type | Dry run | Real run |
|---|---|---|
| `set env`, `scale`, `rollout undo/restart` | `--dry-run=server` as fixer | fixer, then `rollout status` (180 s), then health |
| `find ... -delete` | the same `find` without `-delete`, as reader | fixer, then health |
| `psql` inside a pod (no dry run exists) | the matching check from `PREVIEW`, e.g. `db_read_only` must say `on` | fixer, then health |

## Gotchas

- `/ready` used to check dependencies. A DB problem pulled the pod out of the service and hid the
  real errors.
- Redis on `OrderedReady` couldn't restart a broken pod; switched to `Parallel`.
- The crash-loop alert flapped on pod state; it now uses the restart count.
- Receipts used to stay on disk and filled 64Mi in about 6 minutes under the lag fault's traffic,
  so `disk_full` fired without being injected. Receipts are now deleted once the order is queued.
- `PoolTimeout` takes 30 s to show up in a request, so the health check logs it right away for the
  alerter to pick up.
