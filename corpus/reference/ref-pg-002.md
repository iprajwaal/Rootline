---
id: REF-PG-002
type: ref
title: PostgreSQL connection limits and reserved slots
publisher: PostgreSQL documentation
source_url: https://www.postgresql.org/docs/current/runtime-config-connection.html
licence: PostgreSQL License
attribution: Portions Copyright 1996-2026 The PostgreSQL Global Development Group.
changes: Shortened to the connection limit settings and reworded.
retrieved: "2026-10-19"
topics: [max_connections, connection limit, too many clients, reserved connections, database]
---

## max_connections

Sets the maximum number of concurrent connections to the database server. The default is
typically 100, but can be lower if kernel settings do not support it. It can only be set at server
start. PostgreSQL sizes some resources, including shared memory, from this value, so raising it uses
more memory. A standby server must use the same value or higher than its primary.

At most `max_connections` connections can ever be active at the same time.

## superuser_reserved_connections

The number of connection slots kept for superusers. The default is 3. When the number of active
connections reaches `max_connections` minus this value, only superusers can connect. These slots
are a last reserve for emergencies. It can only be set at server start.

## reserved_connections

The number of slots kept for roles with the privileges of `pg_use_reserved_connections`. The
default is 0. When free slots fall into this range, only superusers and those roles can connect.
It can only be set at server start.
