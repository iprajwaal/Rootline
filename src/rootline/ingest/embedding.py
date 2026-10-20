import base64
import hashlib
import logging
import time
from array import array
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

import httpx
from google import genai
from google.genai import types

from rootline.core.context import request_id
from rootline.core.files import write_atomic
from rootline.llm.ledger import Call, Kind, RecordCall, elapsed_ms

log = logging.getLogger(__name__)

Task = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
# text-embedding-005 takes up to 250 texts a call; 100 keeps each call well under its size cap.
BATCH = 100
# Vertex takes about 12 s to answer the first call on a new connection, so idle connections
# are kept for 10 minutes instead of httpx's 5 seconds.
KEEPALIVE_S = 600
DOCUMENT_ATTEMPTS = 5


class EmbeddingUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Embedder:
    model_name: str
    dims: int
    api_key: str
    vertexai: bool
    cache_dir: Path
    timeout_s: float
    record: RecordCall
    usd_per_mchar: float
    query_attempts: int = 1

    def documents(self, texts: list[str]) -> list[list[float] | None]:
        # A text that cannot be embedded now gets None. Its chunk is stored without a vector
        # and embedded again the next time its file is read.
        found = {text: self.cached(text, "RETRIEVAL_DOCUMENT") for text in texts}
        missing = [text for text, vector in found.items() if vector is None]
        for start in range(0, len(missing), BATCH):
            batch = missing[start : start + BATCH]
            try:
                found.update(zip(batch, self.call(batch, "RETRIEVAL_DOCUMENT"), strict=True))
            except EmbeddingUnavailable as error:
                log.warning("embedding skipped texts=%d error=%s", len(missing) - start, error)
                break
        return [found[text] for text in texts]

    def warm(self) -> None:
        # Opens the connection early: Vertex takes about 12 s to answer the first call on it.
        try:
            self.call(["warm up"], "RETRIEVAL_QUERY")
        except EmbeddingUnavailable as error:
            log.warning("embedding warm-up failed error=%s", error)

    def query(self, text: str) -> list[float]:
        return self.cached(text, "RETRIEVAL_QUERY") or self.call([text], "RETRIEVAL_QUERY")[0]

    def call(self, texts: list[str], task: Task) -> list[list[float]]:
        started = time.perf_counter()
        if not self.api_key:
            self.note("llm_unavailable", task, started, 0, "no_api_key")
            raise EmbeddingUnavailable("no API key set")
        attempts = self.query_attempts if task == "RETRIEVAL_QUERY" else DOCUMENT_ATTEMPTS
        config = types.EmbedContentConfig(task_type=task, output_dimensionality=self.dims)
        contents: list[types.PartUnion] = list(texts)
        try:
            client = vertex_client(self.api_key, self.vertexai, self.timeout_s, attempts)
            response = client.models.embed_content(
                model=self.model_name, contents=contents, config=config
            )
        except Exception as error:
            self.note("llm_error", task, started, 0, type(error).__name__)
            raise EmbeddingUnavailable(f"{self.model_name} call failed: {error}") from error
        vectors = [list(item.values or []) for item in response.embeddings or []]
        if len(vectors) != len(texts) or any(len(v) != self.dims for v in vectors):
            raise EmbeddingUnavailable(f"{self.model_name} returned the wrong vectors")
        billed = response.metadata.billable_character_count if response.metadata else None
        self.note("llm_call", task, started, billed or sum(len(t) for t in texts))
        for text, vector in zip(texts, vectors, strict=True):
            self.store(text, task, vector)
        return vectors

    def cached(self, text: str, task: Task) -> list[float] | None:
        path = self.path(text, task)
        if not path.exists():
            return None
        return list(array("f", base64.b64decode(path.read_text())))

    def store(self, text: str, task: Task, vector: list[float]) -> None:
        # Stored like the LLM answers and committed, so runs without a key get the same vectors.
        write_atomic(
            self.path(text, task), base64.b64encode(array("f", vector).tobytes()).decode() + "\n"
        )

    def path(self, text: str, task: Task) -> Path:
        key = hashlib.sha256(f"{task}\n{self.dims}\n{text}".encode()).hexdigest()
        return self.cache_dir / self.model_name / f"{key}.txt"

    def note(self, kind: Kind, task: Task, started: float, chars: int, *degraded: str) -> None:
        self.record(
            Call(
                request_id=request_id(),
                kind=kind,
                task="embed_query" if task == "RETRIEVAL_QUERY" else "embed_documents",
                model=self.model_name,
                prompt_version=None,
                latency_ms=elapsed_ms(started),
                cost_usd=round(chars * self.usd_per_mchar / 1_000_000, 8),
                degraded=degraded,
            )
        )


@cache
def vertex_client(api_key: str, vertexai: bool, timeout_s: float, attempts: int) -> genai.Client:
    pool = httpx.Client(limits=httpx.Limits(keepalive_expiry=KEEPALIVE_S), timeout=timeout_s)
    options = types.HttpOptions(
        timeout=int(timeout_s * 1000),
        retry_options=types.HttpRetryOptions(attempts=attempts),
        httpx_client=pool,
    )
    return genai.Client(vertexai=vertexai, api_key=api_key, http_options=options)


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
