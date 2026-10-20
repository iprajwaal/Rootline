import logging
import os
import time

import psycopg
from confluent_kafka import Consumer

logging.basicConfig(format="%(asctime)s %(levelname)s [orders-worker] %(message)s", level="INFO")
log = logging.getLogger("orders-worker")
WORK_SECONDS = float(os.environ.get("WORK_SECONDS", "0.05"))


def main() -> None:
    consumer = Consumer(
        {
            "bootstrap.servers": os.environ["KAFKA_BROKERS"],
            "group.id": "orders-worker",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe(["orders"])
    log.info("orders-worker consuming topic orders")
    with connected(os.environ["DATABASE_URL"]) as conn:
        while True:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                log.error("consumer error: %s", message.error())
                continue
            # Each order takes real work (payment capture, stock), so one worker has a limit.
            time.sleep(WORK_SECONDS)
            conn.execute(
                "update orders set status = 'processed' where id = %s", (message.key().decode(),)
            )


def connected(url: str) -> psycopg.Connection:
    # Postgres can start after the worker; waiting here keeps restarts for real failures.
    for attempt in range(30):
        try:
            return psycopg.connect(url, autocommit=True)
        except psycopg.OperationalError as error:
            log.warning("database not ready (attempt %s): %s", attempt + 1, error)
            time.sleep(2)
    raise SystemExit("database never became ready")


if __name__ == "__main__":
    main()
