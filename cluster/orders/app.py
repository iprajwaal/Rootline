import logging
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import psycopg
import redis
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewPartitions, NewTopic
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool, PoolTimeout

logging.basicConfig(format="%(asctime)s %(levelname)s [orders-api] %(message)s", level="INFO")
log = logging.getLogger("orders-api")

TMP = Path(os.environ.get("TMP_DIR", "/var/lib/shoplite/tmp"))
POOL_MAX = int(os.environ.get("DB_POOL_MAX", "20"))
# A full disk stops receipts long before it reaches 100%, so health warns early.
DISK_LIMIT = 0.95
HEALTH_THREADS = anyio.CapacityLimiter(4)


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        # The real crash loop: the container exits, Kubernetes restarts it, again and again.
        log.error("config error: %s is not set", name)
        sys.exit(1)
    return value


PAYMENT_GATEWAY_URL = required("PAYMENT_GATEWAY_URL")
# Several partitions, so more consumers can share the load when one falls behind.
PARTITIONS = int(os.environ.get("ORDER_PARTITIONS", "6"))
pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=POOL_MAX, open=False)
cache = redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=2)
queue = Producer({"bootstrap.servers": os.environ["KAFKA_BROKERS"], "message.timeout.ms": 5000})


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    pool.open(wait=True, timeout=60)
    with pool.connection() as conn:
        conn.execute(
            "create table if not exists orders (id text primary key, item text,"
            " status text not null default 'new', created_at timestamptz default now())"
        )
    TMP.mkdir(parents=True, exist_ok=True)
    order_topic(os.environ["KAFKA_BROKERS"])
    log.info("orders-api ready, pool max %s", POOL_MAX)
    yield
    pool.close()


app = FastAPI(lifespan=lifespan)


@app.get("/ready")
def ready() -> dict[str, str]:
    # Readiness checks this process only. Tying it to the database would pull the pod out of
    # the service whenever the database struggles, and hide the errors users actually see.
    return {"status": "ok"}


@app.get("/health")
async def health() -> JSONResponse:
    # Own threads for the checks, so a jammed request pool cannot stop health from answering.
    checks = {
        "db": db_ok, "db_writes": db_writes_ok, "cache": cache_ok, "queue": queue_ok,
        "disk": disk_ok,
    }  # fmt: skip
    parts = {
        name: await anyio.to_thread.run_sync(check, limiter=HEALTH_THREADS)
        for name, check in checks.items()
    }
    status = "ok" if all(state == "ok" for state in parts.values()) else "degraded"
    return JSONResponse(
        {"status": status, "components": parts}, status_code=200 if status == "ok" else 503
    )


@app.post("/orders")
def create_order(item: str = "book") -> dict[str, Any]:
    order_id = uuid4().hex[:12]
    try:
        with pool.connection(timeout=30) as conn:
            conn.execute("insert into orders (id, item) values (%s, %s)", (order_id, item))
    except PoolTimeout as error:
        log.error("psycopg_pool.PoolTimeout: %s", error)
        raise HTTPException(503, "database busy") from error
    except psycopg.Error as error:
        log.error("%s.%s: %s", type(error).__module__, type(error).__name__, error)
        raise HTTPException(503, "database error") from error
    try:
        cache.set(f"order:{order_id}", item, ex=600)
    except redis.exceptions.ConnectionError as error:
        # Orders still work without the cache, only slower to read back.
        log.error("redis.exceptions.ConnectionError: %s", error)
    receipt = TMP / f"receipt-{order_id}.pdf"
    try:
        receipt.write_bytes(b"%PDF-1.4 receipt " + order_id.encode() + b" " * 4096)
    except OSError as error:
        log.error("OSError: %s", error)
        raise HTTPException(500, "could not write the receipt") from error
    queue.produce("orders", key=order_id, value=item)
    queue.poll(0)
    # The mailer builds the receipt again from the order, so this copy is scratch. Kept, it
    # would fill the volume within hours.
    receipt.unlink(missing_ok=True)
    return {"id": order_id, "status": "new"}


@app.get("/reports/sales")
def sales_report() -> dict[str, Any]:
    # The slow report from RCA-2025-014: it holds a pool connection for the whole query, so
    # enough of them at once leave checkout with no connection to use.
    seconds = float(os.environ.get("REPORT_SECONDS", "20"))
    try:
        with pool.connection(timeout=30) as conn:
            total = conn.execute("select count(*) from orders, pg_sleep(%s)", (seconds,)).fetchone()
    except PoolTimeout as error:
        log.error("psycopg_pool.PoolTimeout: %s", error)
        raise HTTPException(503, "database busy") from error
    return {"orders": total[0] if total else 0}


@app.get("/orders/{order_id}")
def read_order(order_id: str) -> dict[str, Any]:
    try:
        cached = cache.get(f"order:{order_id}")
        if cached is not None:
            return {"id": order_id, "item": cached.decode(), "from": "cache"}
    except redis.exceptions.ConnectionError as error:
        log.error("redis.exceptions.ConnectionError: %s", error)
    with pool.connection(timeout=30) as conn:
        row = conn.execute("select item, status from orders where id = %s", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such order")
    return {"id": order_id, "item": row[0], "status": row[1], "from": "db"}


def order_topic(brokers: str) -> None:
    admin = AdminClient({"bootstrap.servers": brokers})
    topics = admin.list_topics(timeout=10).topics
    if "orders" not in topics:
        admin.create_topics([NewTopic("orders", num_partitions=PARTITIONS)])["orders"].result()
    elif len(topics["orders"].partitions) < PARTITIONS:
        admin.create_partitions([NewPartitions("orders", PARTITIONS)])["orders"].result()


def db_ok() -> str:
    try:
        with pool.connection(timeout=2) as conn:
            conn.execute("select 1")
        return "ok"
    except PoolTimeout as error:
        # Logged here too, so the error shows within seconds rather than after a request's
        # full 30 seconds of waiting.
        log.error("psycopg_pool.PoolTimeout: %s", error)
        return "failing"
    except Exception as error:
        log.error("database check failed: %s", error)
        return "failing"


def db_writes_ok() -> str:
    # A database can answer reads and still refuse every write, as when maintenance left it
    # read-only.
    try:
        with pool.connection(timeout=2) as conn:
            row = conn.execute("show transaction_read_only").fetchone()
    except Exception:
        # A database that cannot be reached at all is the db check's finding.
        return "unknown"
    return "ok" if row and row[0] == "off" else "failing"


def cache_ok() -> str:
    try:
        return "ok" if cache.ping() else "failing"
    except redis.exceptions.RedisError:
        return "failing"


def queue_ok() -> str:
    try:
        queue.list_topics(timeout=2)
        return "ok"
    except Exception:
        return "failing"


def disk_ok() -> str:
    usage = shutil.disk_usage(TMP)
    return "ok" if usage.used / usage.total < DISK_LIMIT else "failing"
