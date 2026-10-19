from pathlib import Path
from typing import Any, Literal

import psycopg

from rootline.catalog import load_catalog
from rootline.config import Settings
from rootline.ingest.analysis import Analyzer
from rootline.ingest.classify import LlmVerdict
from rootline.ingest.embedding import Embedder
from rootline.ingest.extract import Record
from rootline.ingest.parsing import PARSERS
from rootline.ingest.store import Outcome, Store
from rootline.llm.structured import StructuredLLM
from rootline.models import RcaRecord, SopRecord


def build_analyzer(settings: Settings) -> Analyzer:
    llm = StructuredLLM(settings.llm_small_model, settings.google_api_key, settings.llm_cache_dir)

    def ask_label(prompt: str) -> LlmVerdict:
        return llm.ask("classify", prompt, LlmVerdict)

    def ask_record(prompt: str, label: Literal["sop", "rca"]) -> Record:
        schema: type[Record] = SopRecord if label == "sop" else RcaRecord
        return llm.ask(f"extract_{label}", prompt, schema)

    return Analyzer(
        catalog=load_catalog(settings.catalog_path),
        embedder=Embedder(settings.embed_model, settings.model_cache_dir),
        ask_label=ask_label,
        ask_record=ask_record,
    )


def build_store(conn: psycopg.Connection[Any], settings: Settings, analyzer: Analyzer) -> Store:
    return Store(conn, analyzer, settings.raw_dir, settings.embed_model)


def ingest_path(store: Store, path: Path, sync: bool = False) -> list[Outcome]:
    if path.is_file():
        files = [path]
    else:
        files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in PARSERS)
    outcomes = [store.ingest(file.as_posix(), file.read_bytes()) for file in files]
    if sync and path.is_dir():
        prefix = path.as_posix().rstrip("/") + "/"
        outcomes += store.sync_removed(prefix, {file.as_posix() for file in files})
    return outcomes
