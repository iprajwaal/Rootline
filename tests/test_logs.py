import json
import logging

from rootline.core.context import request_scope
from rootline.core.logs import JsonFormatter


def record(message: str) -> logging.LogRecord:
    return logging.LogRecord("rootline.test", logging.WARNING, __file__, 1, message, None, None)


def test_log_line_is_json_with_level_logger_and_message() -> None:
    entry = json.loads(JsonFormatter().format(record("ledger write failed task=classify")))

    assert (entry["level"], entry["logger"], entry["msg"]) == (
        "warning",
        "rootline.test",
        "ledger write failed task=classify",
    )
    assert "request_id" not in entry


def test_log_line_inside_a_request_carries_its_id() -> None:
    with request_scope("req-9"):
        entry = json.loads(JsonFormatter().format(record("rca answer degraded")))

    assert entry["request_id"] == "req-9"
