from pathlib import Path
from typing import Any, Literal

import psycopg

from rootline.catalog import load_catalog
from rootline.config import Settings
from rootline.core.models import RcaRecord, SopRecord
from rootline.ingest.analysis import Analyzer
from rootline.ingest.classify import CLASSIFY_PROMPT_VERSION, AskLlm, LlmVerdict
from rootline.ingest.embedding import Embedder
from rootline.ingest.extract import EXTRACT_PROMPT_VERSION, AskRecord, Record
from rootline.ingest.learned import labelled_examples, train
from rootline.ingest.parsing import PARSERS
from rootline.ingest.store import Outcome, Store
from rootline.llm.ledger import PostgresLedger, RecordCall
from rootline.llm.structured import StructuredLLM, build_llm


def build_analyzer(settings: Settings, record: RecordCall | None = None) -> Analyzer:
    llm = build_llm(settings, record or PostgresLedger(settings.database_url).record)
    return Analyzer(
        catalog=load_catalog(settings.catalog_path),
        embedder=Embedder(settings.embed_model, settings.model_cache_dir),
        ask_label=label_asker(llm),
        ask_record=record_asker(llm),
        first=train(labelled_examples(settings.corpus_dir)),
    )


def label_asker(llm: StructuredLLM) -> AskLlm:
    def ask_label(prompt: str) -> LlmVerdict:
        return llm.ask("classify", prompt, LlmVerdict, CLASSIFY_PROMPT_VERSION)

    return ask_label


def record_asker(llm: StructuredLLM) -> AskRecord:
    def ask_record(prompt: str, label: Literal["sop", "rca"]) -> Record:
        schema: type[Record] = SopRecord if label == "sop" else RcaRecord
        return llm.ask(f"extract_{label}", prompt, schema, EXTRACT_PROMPT_VERSION)

    return ask_record


def build_store(
    conn: psycopg.Connection[Any], settings: Settings, analyzer: Analyzer, actor: str
) -> Store:
    return Store(conn, analyzer, settings.raw_dir, settings.embed_model, actor)


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
