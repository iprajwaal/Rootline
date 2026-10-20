from dataclasses import dataclass
from typing import Any

import psycopg

from rootline.catalog import Catalog
from rootline.ingest.embedding import Embedder
from rootline.retrieval.documents import Document, read_document
from rootline.retrieval.passages import Passage, top_passages
from rootline.retrieval.references import Reference, references
from rootline.retrieval.search import Searcher, SearchResponse
from rootline.retrieval.suggest import Suggester, SuggestResponse


@dataclass(frozen=True)
class Knowledge:
    database_url: str
    embedder: Embedder
    catalog: Catalog

    def search(self, text: str) -> SearchResponse:
        with psycopg.connect(self.database_url) as conn:
            return self.searcher(conn).search(text)

    def suggest(self, text: str) -> SuggestResponse:
        with psycopg.connect(self.database_url) as conn:
            return Suggester(self.searcher(conn), self.catalog.alerts).suggest(text)

    def document(self, doc_id: str) -> Document | None:
        with psycopg.connect(self.database_url) as conn:
            return read_document(conn, doc_id)

    def passages(
        self, question: str, doc_ids: list[str], doc_type: str, limit: int
    ) -> list[Passage]:
        with psycopg.connect(self.database_url) as conn:
            return top_passages(conn, self.embedder, question, doc_ids, doc_type, limit)

    def references(self, text: str) -> list[Reference]:
        with psycopg.connect(self.database_url) as conn:
            return references(conn, text)

    def searcher(self, conn: psycopg.Connection[Any]) -> Searcher:
        return Searcher(conn, self.embedder, self.catalog.services)
