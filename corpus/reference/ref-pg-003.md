---
id: REF-PG-003
type: ref
title: See what database connections are doing with pg_stat_activity
publisher: PostgreSQL documentation
source_url: https://www.postgresql.org/docs/current/monitoring-stats.html
licence: PostgreSQL License
attribution: Portions Copyright 1996-2026 The PostgreSQL Global Development Group.
changes: Shortened to the pg_stat_activity view and reworded. The example query is added.
retrieved: "2026-10-19"
topics: [pg_stat_activity, connections, long running queries, idle in transaction, database]
---

## Summary

The `pg_stat_activity` view has one row per server process, with information about what that
process is doing now.

## state column

- `active`: the backend is running a query.
- `idle`: the backend is waiting for a new command from the client.
- `idle in transaction`: the backend is inside a transaction but not running a query.
- `idle in transaction (aborted)`: like `idle in transaction`, but a statement in the transaction
  failed.

## Other useful columns

- `query_start`: when the current query started, or when the last query started if the state is
  not `active`.
- `wait_event`: what the backend is waiting for, or empty if it is not waiting.

## Example

List connections by how long their current or last query has run:

```sql
select pid, state, now() - query_start as age, wait_event, query
from pg_stat_activity
order by age desc;
```
