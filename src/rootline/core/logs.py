import json
import logging
from datetime import UTC, datetime

from rootline.core.context import REQUEST_ID


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if request := REQUEST_ID.get():
            entry["request_id"] = request
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(level: str) -> None:
    # Only our own loggers: uvicorn and the LangGraph server keep their own setup.
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("rootline")
    logger.handlers[:] = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False
