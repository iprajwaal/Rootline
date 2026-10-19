import logging
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import UUID

import psycopg
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult

log = logging.getLogger(__name__)

# Set once per API request or CLI run, so all LLM calls it makes share one ID.
REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)

INSERT = """
    insert into ledger (request_id, kind, task, model, prompt_version, input_tokens,
        output_tokens, cost_usd, latency_ms, degraded)
    values (%(request_id)s, %(kind)s, %(task)s, %(model)s, %(prompt_version)s,
        %(input_tokens)s, %(output_tokens)s, %(cost_usd)s, %(latency_ms)s, %(degraded)s)
"""

Kind = Literal["llm_call", "llm_cache", "llm_unavailable", "llm_error"]


@dataclass(frozen=True)
class Call:
    request_id: str
    kind: Kind
    task: str | None
    model: str
    prompt_version: str | None
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    degraded: tuple[str, ...] = ()


RecordCall = Callable[[Call], None]


@dataclass(frozen=True)
class Prices:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        spent = input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok
        return round(spent / 1_000_000, 6)


@dataclass(frozen=True)
class PostgresLedger:
    database_url: str

    def record(self, call: Call) -> None:
        try:
            with psycopg.connect(self.database_url, autocommit=True) as conn:
                conn.execute(INSERT, asdict(call) | {"degraded": list(call.degraded)})
        except psycopg.Error as error:
            # A lost ledger row must not fail the answer it describes.
            log.warning("ledger write failed task=%s error=%s", call.task, error)


class LedgerCallback(BaseCallbackHandler):
    def __init__(self, record: RecordCall, model: str, prices: Prices) -> None:
        self.record = record
        self.model = model
        self.prices = prices
        self.runs: dict[UUID, tuple[float, dict[str, Any]]] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.runs[run_id] = (time.perf_counter(), metadata or {})

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        started, metadata = self.runs.pop(run_id)
        input_tokens, output_tokens = usage(response)
        self.record(self.call(started, metadata, "llm_call", input_tokens, output_tokens))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        started, metadata = self.runs.pop(run_id)
        self.record(self.call(started, metadata, "llm_error", degraded=(type(error).__name__,)))

    def call(
        self,
        started: float,
        metadata: dict[str, Any],
        kind: Kind,
        input_tokens: int = 0,
        output_tokens: int = 0,
        degraded: tuple[str, ...] = (),
    ) -> Call:
        return Call(
            request_id=metadata.get("request_id") or request_id(),
            kind=kind,
            task=metadata.get("task"),
            model=self.model,
            prompt_version=metadata.get("prompt_version"),
            latency_ms=elapsed_ms(started),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self.prices.cost(input_tokens, output_tokens),
            degraded=degraded,
        )


def usage(response: LLMResult) -> tuple[int, int]:
    generation = response.generations[0][0] if response.generations else None
    message = generation.message if isinstance(generation, ChatGeneration) else None
    if isinstance(message, AIMessage) and message.usage_metadata:
        return message.usage_metadata["input_tokens"], message.usage_metadata["output_tokens"]
    return 0, 0


def request_id() -> str:
    return REQUEST_ID.get() or uuid.uuid4().hex


def elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
