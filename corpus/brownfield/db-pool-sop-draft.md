| Document title | Recover orders-api from database connection pool exhaustion |
| --- | --- |
| Document number | TBD |
| Revision | 02 |
| Effective date | 2026-07-14 |
| Owner | Orders team |
| Services | orders-api, orders-db |
| Alerts | OrdersDbPoolExhausted |
| Supersedes | None |

# Standard operating procedure

## Recover orders-api from database connection pool exhaustion

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

TBD

## 2. Scope

TBD

## 3. When to use

Use this procedure when the OrdersDbPoolExhausted alert fires, or when orders-api returns 503 and logs "psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec".

Alerts: OrdersDbPoolExhausted

## 4. Definitions and abbreviations

TBD

## 5. Roles and responsibilities

TBD

## 6. Related documents

TBD

## 7. Prerequisites

- kubectl access to the shop namespace.
- Read access to orders-db with the shop user.
- orders-db max_connections is at least 300. Ask the database on-call if unsure.

## 8. Procedure overview

Confirm the API is failing on pool timeouts, find what is holding connections, raise the pool limit to restore service, then verify. Fixing the query that held the connections is follow-up work and is not part of this procedure.

## 9. Procedure

Run the steps in order. Do not skip or reorder steps. Stop and escalate if a result does not match the expected result.

| Step | Action | Command | Expected result | Responsibility | Automation |
| --- | --- | --- | --- | --- | --- |
| 1 | Confirm the API is failing on pool timeouts. | `kubectl -n shop logs deploy/orders-api --since=15m` | Repeated PoolTimeout lines in the last 15 minutes. | On-call engineer | check:logs |
| 2 | Count open database connections. | `psql -h orders-db -U shop -c "select count(*) from pg_stat_activity"` | The count is at or near 20 times the number of orders-api pods. | On-call engineer | check:db_connections |
| 3 | List long running queries that hold connections. | `psql -h orders-db -U shop -c "select pid, now() - query_start as age, query from pg_stat_activity where state <> 'idle' order by age desc limit 10"` | Any query older than 60 seconds is posted in the incident channel with its pid. | On-call engineer |  |
| 4 | Raise the pool limit. | `kubectl -n shop set env deploy/orders-api DB_POOL_MAX=50` | The rollout completes and new pods start with DB_POOL_MAX=50. | On-call engineer | action:raise_pool_limit |
| 5 | Confirm the API is healthy. | `curl -s http://orders-api:8080/health` | Status ok and orders-db ok. | On-call engineer | check:health |

## 10. Verification

- The 5xx rate on orders-api stays under 1% for 10 minutes.
- Open connections stay below 80% of the new pool size.
- No new PoolTimeout lines in orders-api logs.

## 11. Rollback

| Step | Action | Command | Expected result | Responsibility | Automation |
| --- | --- | --- | --- | --- | --- |
| 1 | Set the pool limit back once the long running queries are fixed. | `kubectl -n shop set env deploy/orders-api DB_POOL_MAX=20` | The rollout completes with DB_POOL_MAX=20 and the error rate stays under 1%. | On-call engineer |  |

## 12. Troubleshooting

TBD

## 13. Escalation

- Page the orders team lead if the error rate is above 5% after step 5.
- Page the database on-call if orders-db rejects connections or a query cannot be stopped.

## 14. Security considerations

- Use the shop user for diagnostics. Do not use the postgres superuser.
- Do not paste query text that contains customer data into public channels.

## 15. Compliance and standards

TBD

## 16. Review and update

TBD

## 17. Records

TBD

## Appendix A. Revision history

TBD
