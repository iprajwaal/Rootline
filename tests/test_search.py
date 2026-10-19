import statistics

import psycopg
import pytest
from fastapi.testclient import TestClient

from rootline.api.app import app, get_analyzer, get_settings
from rootline.config import Settings
from rootline.ingest.analysis import Analyzer
from rootline.retrieval.search import Searcher, SearchResponse


@pytest.fixture(scope="module")
def searcher(search_db: psycopg.Connection, analyzer: Analyzer) -> Searcher:
    return Searcher(search_db, analyzer.embedder, analyzer.catalog.services)


def top(response: SearchResponse, lane: str) -> str | None:
    hits = getattr(response, lane).hits
    return hits[0].doc_id if hits else None


def all_hits(response: SearchResponse) -> list[str]:
    return [hit.doc_id for hit in [*response.rca.hits, *response.sop.hits]]


@pytest.mark.parametrize(
    ("query", "doc_id"),
    [
        ("SOP-DB-001", "SOP-DB-001"),
        ("show me rca-2025-014 please", "RCA-2025-014"),
        ("sop-cache-001", "SOP-CACHE-001"),
    ],
)
def test_id_returns_exactly_that_document(searcher: Searcher, query: str, doc_id: str) -> None:
    response = searcher.search(query)

    assert response.mode == "id"
    assert all_hits(response) == [doc_id]


def test_unknown_id_is_reported_with_labelled_neighbours(searcher: Searcher) -> None:
    response = searcher.search("SOP-DB-009")

    assert all_hits(response) == []
    assert response.message == "No document has the ID SOP-DB-009."
    assert "SOP-DB-001" in [hit.doc_id for hit in response.did_you_mean]
    assert all("not the same document" in hit.evidence[0] for hit in response.did_you_mean)


def test_exact_title_returns_that_document(searcher: Searcher) -> None:
    response = searcher.search("Recover orders-api when the cache is down")

    assert (response.mode, all_hits(response)) == ("title", ["SOP-CACHE-001"])


def test_close_titles_stay_visible_next_to_the_exact_one(searcher: Searcher) -> None:
    response = searcher.search("consumer lag on the orders queue")

    assert top(response, "sop") == "SOP-QUEUE-001"
    alternatives = {hit.doc_id for hit in response.did_you_mean}
    assert "SOP-QUEUE-002" in alternatives
    assert any(hit.doc_type == "rca" for hit in response.did_you_mean)


@pytest.mark.parametrize(
    ("query", "rca", "sop"),
    [
        (
            "psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec",
            "RCA-2025-014",
            "SOP-DB-001",
        ),
        ("CrashLoopBackOff", "RCA-2026-007", "SOP-DEPLOY-002"),
        ("redis", "RCA-2025-031", "SOP-CACHE-001"),
        (
            "the time checkout got slow at night because the cache pod was evicted",
            "RCA-2025-031",
            None,
        ),
    ],
)
def test_error_service_and_description_queries_find_the_right_documents(
    searcher: Searcher, query: str, rca: str, sop: str | None
) -> None:
    response = searcher.search(query)

    assert top(response, "rca") == rca
    if sop:
        assert top(response, "sop") == sop


@pytest.mark.parametrize(
    "query",
    [
        "kafka consumer lag",
        "cache",
        "OOMKilled",
        "how do I roll back a bad deploy",
        "orders-worker",
    ],
)
def test_lanes_never_mix_document_types(searcher: Searcher, query: str) -> None:
    response = searcher.search(query)

    assert all(hit.doc_type == "rca" for hit in response.rca.hits)
    assert all(hit.doc_type == "sop" for hit in response.sop.hits)


def test_brownfield_copies_collapse_into_the_canonical_document(searcher: Searcher) -> None:
    response = searcher.search("raise the database pool limit DB_POOL_MAX")

    assert top(response, "sop") == "SOP-DB-001"
    assert response.sop.hits[0].similar_copies >= 3
    assert not any(hit.doc_id.startswith("SOP-B-") for hit in response.sop.hits[:1])


@pytest.mark.parametrize(
    "query",
    ["how do I bake sourdough bread", "configure SSO for the HR portal", "No space left on device"],
)
def test_unrelated_queries_return_nothing_relevant(searcher: Searcher, query: str) -> None:
    response = searcher.search(query)

    assert response.rca.nothing_relevant and response.sop.nothing_relevant


def test_superseded_sop_comes_after_the_current_one(searcher: Searcher) -> None:
    hits = searcher.search("kafka consumer lag").sop.hits

    assert [hit.doc_id for hit in hits[:2]] == ["SOP-QUEUE-002", "SOP-QUEUE-001"]
    assert hits[1].status == "superseded"


def test_hits_carry_linked_documents_from_the_other_lane(searcher: Searcher) -> None:
    hit = searcher.search("SOP-DB-001").sop.hits[0]

    assert ("RCA-2025-014", "rca") in [(link.doc_id, link.doc_type) for link in hit.linked]


def test_search_p95_latency_is_under_300_ms(searcher: Searcher) -> None:
    queries = [
        "SOP-DB-001",
        "kafka consumer lag",
        "redis",
        "CrashLoopBackOff",
        "roll back a deploy",
    ]
    searcher.search("warm up")

    latencies = [searcher.search(query).latency_ms for query in queries * 6]

    assert statistics.quantiles(latencies, n=20)[18] < 300


def test_search_endpoint(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    settings = Settings(database_url=search_db_url, google_api_key="")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analyzer] = lambda: analyzer

    response = TestClient(app).get("/search", params={"q": "SOP-DB-001"})
    empty = TestClient(app).get("/search", params={"q": ""})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["sop"]["hits"][0]["doc_id"] == "SOP-DB-001"
    assert empty.status_code == 422
