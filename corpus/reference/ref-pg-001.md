---
id: REF-PG-001
type: ref
title: PostgreSQL disk full failure
publisher: PostgreSQL documentation
source_url: https://www.postgresql.org/docs/current/diskusage.html#DISK-FULL
licence: PostgreSQL License
attribution: Portions Copyright 1996-2026 The PostgreSQL Global Development Group.
changes: Reworded slightly.
retrieved: "2026-10-19"
topics: [disk full, WAL, tablespaces, disk quota, database]
---

## Summary

The most important disk monitoring task for a database administrator is to make sure the disk does
not become full. A full data disk does not corrupt data, but it can stop useful work. If the disk
holding the WAL files fills up, the database server can panic and shut down.

## What to do

If you cannot free space on the disk by deleting other things, move some database files to other
file systems with tablespaces.

Some file systems perform badly when they are almost full, so act before the disk is completely
full.

## Disk quotas

If the system uses per-user disk quotas, the database is subject to the quota of the user the
server runs as. Going over the quota has the same effect as running out of disk space.
