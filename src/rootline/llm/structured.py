import hashlib
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables.config import ensure_config, merge_configs
from pydantic import BaseModel

from rootline.config import Settings
from rootline.llm.ledger import (
    Call,
    Kind,
    LedgerCallback,
    Prices,
    RecordCall,
    elapsed_ms,
    request_id,
)
from rootline.llm.tracing import tracing_callbacks


class LLMUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class StructuredLLM:
    model: str
    api_key: str
    cache_dir: Path
    record: RecordCall
    prices: Prices
    timeout_s: float
    max_retries: int
    vertexai: bool = False
    tracing: tuple[BaseCallbackHandler, ...] = ()

    def ask[T: BaseModel](self, task: str, prompt: str, schema: type[T], prompt_version: str) -> T:
        started = time.perf_counter()
        request = request_id()
        # Answers are cached by task, model and prompt. The cache is committed, so a run
        # without an API key gives the same results as the run that filled it.
        key = hashlib.sha256(f"{task}\n{self.model}\n{prompt}".encode()).hexdigest()
        path = self.cache_dir / task / f"{key}.json"
        if path.exists():
            self.note(request, "llm_cache", task, prompt_version, started)
            return schema.model_validate_json(path.read_text())
        if not self.api_key:
            self.note(request, "llm_unavailable", task, prompt_version, started, "no_api_key")
            raise LLMUnavailable("no API key set")

        # Merged into the inherited config, so inside a graph node the call nests under it.
        metadata = {"task": task, "prompt_version": prompt_version, "request_id": request}
        config = merge_configs(
            ensure_config(),
            {"run_name": task, "metadata": metadata, "callbacks": list(self.tracing)},
        )
        try:
            result = self.chat.with_structured_output(schema).invoke(prompt, config)
        except Exception as error:
            raise LLMUnavailable(f"{self.model} call failed: {error}") from error
        if not isinstance(result, schema):
            raise LLMUnavailable(f"{self.model} returned {type(result).__name__}")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2) + "\n")
        return result

    @cached_property
    def chat(self) -> BaseChatModel:
        # The ledger callback sits on the model, so every call to it is recorded once.
        return init_chat_model(
            self.model,
            model_provider="google_genai",
            api_key=self.api_key,
            timeout=self.timeout_s,
            max_retries=self.max_retries,
            vertexai=self.vertexai,
            callbacks=[LedgerCallback(self.record, self.model, self.prices)],
        )

    def note(
        self,
        request: str,
        kind: Kind,
        task: str,
        prompt_version: str,
        started: float,
        *degraded: str,
    ) -> None:
        self.record(
            Call(
                request_id=request,
                kind=kind,
                task=task,
                model=self.model,
                prompt_version=prompt_version,
                latency_ms=elapsed_ms(started),
                degraded=degraded,
            )
        )


def build_llm(settings: Settings, record: RecordCall) -> StructuredLLM:
    return StructuredLLM(
        model=settings.llm_small_model,
        api_key=settings.google_api_key,
        cache_dir=settings.llm_cache_dir,
        record=record,
        prices=Prices(settings.llm_input_usd_per_mtok, settings.llm_output_usd_per_mtok),
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
        vertexai=settings.llm_vertexai,
        tracing=tuple(tracing_callbacks(settings)),
    )
