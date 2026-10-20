import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# Set once per API request, CLI run or chat turn, so the LLM calls, audit rows and log lines
# it causes share one ID.
REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)


def request_id() -> str:
    return REQUEST_ID.get() or uuid.uuid4().hex


@contextmanager
def request_scope(request: str) -> Iterator[None]:
    token = REQUEST_ID.set(request)
    try:
        yield
    finally:
        REQUEST_ID.reset(token)
