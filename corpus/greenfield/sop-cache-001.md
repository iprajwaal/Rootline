| Document title | Recover orders-api when the cache is down |
| --- | --- |
| Document number | SOP-CACHE-001 |
| Revision | 01 |
| Effective date | 2026-02-03 |
| Owner | Platform team |
| Services | cache, orders-api |
| Alerts | OrdersApiHighLatency |
| Supersedes | None |

# Standard operating procedure

## Recover orders-api when the cache is down

## Table of contents

1. Purpose
2. Scope
3. When to use
4. Definitions and abbreviations
5. Roles and responsibilities
6. Related documents
7. Prerequisites
8. Procedure overview
9. Procedure
10. Verification
11. Rollback
12. Troubleshooting
13. Escalation
14. Security considerations
15. Compliance and standards
16. Review and update
17. Records

Appendix A. Revision history

## 1. Purpose

Bring the cache back when orders-api cannot reach it. Without the cache every read goes to orders-db and checkout gets slow.

## 2. Scope

Applies to the cache statefulset used by orders-api in the shop-prod cluster. Does not apply to lost cache data. The cache holds nothing that is not also in orders-db.

## 3. When to use

Use this procedure when the OrdersApiHighLatency alert fires and orders-api logs show "redis.exceptions.ConnectionError: Error 111 connecting to cache:6379. Connection refused."

Alerts: OrdersApiHighLatency

## 4. Definitions and abbreviations

- Cache: The Redis instance orders-api uses for product and price reads.
- StatefulSet: The Kubernetes object that runs the cache pod with a fixed name and volume.
- PodDisruptionBudget: A rule that stops Kubernetes from evicting too many pods of a service at once.

## 5. Roles and responsibilities

| Role | Responsibility |
| --- | --- |
| On-call engineer | Runs the procedure and posts each result in the incident channel. |
| Platform on-call | Fixes node and volume problems that stop the cache pod from starting. |

## 6. Related documents

- RCA-2025-031: slow checkout while the cache pod was evicted during a node drain.

## 7. Prerequisites

- kubectl access to the shop namespace.

## 8. Procedure overview

Confirm the cache does not answer and that orders-api is falling back to the database, restart the cache, then confirm it answers and latency recovers.

## 9. Procedure

Run the steps in order. Do not skip or reorder steps. Stop and escalate if a result does not match the expected result.

| Step | Action | Command | Expected result | Responsibility | Automation |
| --- | --- | --- | --- | --- | --- |
| 1 | Confirm the cache does not answer. | `redis-cli -h cache ping` | Connection refused instead of PONG. | On-call engineer | check:cache_ping |
| 2 | Confirm orders-api is falling back to the database. | `kubectl -n shop logs deploy/orders-api --since=15m` | ConnectionError lines for cache:6379. | On-call engineer | check:logs |
| 3 | Restart the cache. | `kubectl -n shop rollout restart statefulset/cache` | The cache pod is running and ready. | On-call engineer | action:restart_cache |
| 4 | Confirm the cache answers again. | `redis-cli -h cache ping` | PONG. | On-call engineer | check:cache_ping |

## 10. Verification

- p95 latency on orders-api is back under 300 ms.
- No new ConnectionError lines in orders-api logs for 5 minutes.

## 11. Rollback

None.

## 12. Troubleshooting

| Symptom | Action |
| --- | --- |
| The cache pod stays Pending after the restart. | Its volume may be bound to a drained node. Run kubectl -n shop describe pod cache-0 and page the platform on-call. |
| The cache answers but latency stays high. | The cache is cold. Latency should recover within 10 minutes as keys fill again. |

## 13. Escalation

- Page the platform on-call if the cache pod is not ready 10 minutes after step 3.

## 14. Security considerations

- Do not run FLUSHALL. It clears every key and sends all reads to orders-db at once.

## 15. Compliance and standards

- Restarting the cache is a standard change. Record it in the incident ticket.

## 16. Review and update

Reviewed every 6 months.

## 17. Records

Each run is recorded in the incident ticket.

## Appendix A. Revision history

| Revision | Date | Author | Change |
| --- | --- | --- | --- |
| 01 | 2026-02-03 | Omar Haddad | First version, written after RCA-2025-031. |
