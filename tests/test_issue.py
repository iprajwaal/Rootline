from rootline.retrieval.issue import clean, compose_query, error_lines


def test_ids_times_and_addresses_are_removed_and_short_codes_stay() -> None:
    line = (
        "2026-10-20T03:14:07.123Z ERROR [orders-api-7d9f8b6c5-x2klm]"
        " req=7f3c9a2e-1b2c-4d5e-8f90-123456789abc from 10.4.2.17:51234 trace 0x7ffd3a2b"
        " sha 9f86d081884c7d659a2feaa0c55ad015 order 88123 exit code 137 status 503"
    )

    assert clean(line) == ["[orders-api] from trace sha order exit code 137 status 503"]


def test_stack_frames_are_dropped_and_the_error_is_kept() -> None:
    trace = """Traceback (most recent call last):
  File "/app/orders/db.py", line 88, in get_conn
    conn = pool.getconn(timeout=30)
           ^^^^^^^^^^^^^^^^^^^^^^^^
psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec
Exception in thread "main" java.lang.IllegalStateException: pool closed
\tat com.shop.orders.Db.get(Db.java:42)
\t... 12 more"""

    assert clean(trace) == [
        "psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec",
        'Exception in thread "main" java.lang.IllegalStateException: pool closed',
    ]


def test_repeated_lines_collapse_into_one() -> None:
    log = "\n".join(
        f"2026-10-20T03:14:0{second}Z ERROR [orders-worker] OOMKilled (exit code 137)"
        for second in range(5)
    )

    assert clean(log) == ["[orders-worker] OOMKilled (exit code 137)"]


def test_info_lines_are_dropped_only_when_louder_lines_exist() -> None:
    noisy = "03:14:06 INFO health ok\n03:14:07 ERROR redis ConnectionError"

    assert clean(noisy) == ["redis ConnectionError"]
    assert clean("03:14:06 INFO health ok") == ["health ok"]


def test_query_takes_one_line_per_error_without_the_log_source() -> None:
    lines = [
        "[orders-api] GET /orders failed from",
        "[orders-api] psycopg_pool.PoolTimeout: couldn't get a connection",
        "psycopg_pool.PoolTimeout: couldn't get a connection",
    ]
    errors = ["psycopg_pool.PoolTimeout: couldn't get a connection"]

    query = compose_query(["OrdersDbPoolExhausted", *error_lines(lines, errors)], drop=[])

    assert query == "OrdersDbPoolExhausted psycopg_pool.PoolTimeout: couldn't get a connection"


def test_text_without_a_known_error_is_used_whole() -> None:
    lines = ["consumer group orders-worker lag= on topic orders", "lag still growing"]

    assert error_lines(lines, []) == lines


def test_dropped_alert_names_leave_the_query() -> None:
    parts = ["OrdersApiDiskFull", "OrdersApiDiskFull firing: No space left on device"]

    assert compose_query(parts, drop=["OrdersApiDiskFull"]) == "firing: No space left on device"
