import psycopg

from rootline.ingest.analysis import Analyzer
from rootline.retrieval.retriever import LaneRetriever
from rootline.retrieval.search import Lane


def retriever(url: str, analyzer: Analyzer, lane: Lane) -> LaneRetriever:
    return LaneRetriever(
        database_url=url,
        embedder=analyzer.embedder,
        services=analyzer.catalog.services,
        lane=lane,
    )


def test_retriever_returns_langchain_documents_for_its_list(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    [doc] = retriever(search_db_url, analyzer, "sop").invoke("SOP-DB-001")

    assert (doc.metadata["doc_id"], doc.metadata["doc_type"]) == ("SOP-DB-001", "sop")
    assert doc.page_content


def test_rca_retriever_never_returns_sops(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    docs = retriever(search_db_url, analyzer, "rca").invoke("redis connection refused")

    assert docs and all(doc.metadata["doc_type"] == "rca" for doc in docs)
