import statistics
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient

from rootline.api.app import app, get_analyzer, get_settings
from rootline.config import Settings
from rootline.ingest.analysis import Analyzer
from rootline.retrieval.search import Searcher
from rootline.retrieval.suggest import Suggester, Suggestion, SuggestResponse

CATALOG = Path(__file__).parents[1] / "corpus" / "catalog.yaml"
FAULTS: dict[str, dict[str, Any]] = yaml.safe_load(CATALOG.read_text())["faults"]
# Brownfield documents get minted IDs, so those are checked by title.
EXPECTED = {
    "db_pool_exhausted": ("RCA-2025-014", "SOP-DB-001"),
    "queue_consumer_lag": ("RCA-2025-022", "SOP-QUEUE-002"),
    "cache_down": ("RCA-2025-031", "SOP-CACHE-001"),
    "payment_key_expired": (
        "checkout payments failing after the payment api key expired",
        "Rotate an expired payment-gateway API key",
    ),
    "disk_full": (None, None),
    "bad_config_deploy": ("RCA-2026-007", "SOP-DEPLOY-002"),
    "worker_oom": (
        "orders-worker killed for memory during the month-end batch",
        "recover orders-worker from out of memory kills",
    ),
}


@pytest.fixture(scope="module")
def suggester(search_db: psycopg.Connection, analyzer: Analyzer) -> Suggester:
    searcher = Searcher(search_db, analyzer.embedder, analyzer.catalog.services)
    return Suggester(searcher, analyzer.catalog.alerts)


def alert_text(fault: str) -> str:
    return f"{FAULTS[fault]['alert']}\n{FAULTS[fault]['error']}"


def noisy(fault: str) -> str:
    pod = f"{FAULTS[fault]['service']}-7d9f8b6c5-x2klm"
    error = FAULTS[fault]["error"]
    return "\n".join(
        [
            f"\x1b[2m2026-10-20T03:14:06.002Z\x1b[0m INFO [{pod}] GET /healthz 200 in 3ms",
            f"2026-10-20T03:14:07.123Z ERROR [{pod}] pid=4412"
            " req=7f3c9a2e-1b2c-4d5e-8f90-123456789abc from 10.4.2.17:51234",
            "Traceback (most recent call last):",
            '  File "/app/shoplite/handlers.py", line 88, in handle',
            "    result = run(request)",
            f"2026-10-20T03:14:07.200Z ERROR [{pod}] {error}",
            f"2026-10-20T03:14:08.551Z ERROR [{pod}] trace_id=0x7ffd3a2b9c1e {error}",
            f"Oct 20 03:14:10 node-3 kubelet[1180]: pod {pod} sha=9f86d081884c7d659a2feaa0c55ad015",
        ]
    )


def top(response: SuggestResponse, lane: str) -> Suggestion | None:
    suggestions = getattr(response, lane).suggestions
    return suggestions[0] if suggestions else None


def names(suggestion: Suggestion | None) -> set[str | None]:
    return {suggestion.doc_id, suggestion.title} if suggestion else {None}


@pytest.mark.parametrize("fault", FAULTS)
def test_each_fault_gets_its_own_rca_and_sop(suggester: Suggester, fault: str) -> None:
    response = suggester.suggest(alert_text(fault))
    rca, sop = EXPECTED[fault]

    assert rca in names(top(response, "rca"))
    assert sop in names(top(response, "sop"))


@pytest.mark.parametrize("fault", FAULTS)
def test_noisy_log_gives_the_same_top_suggestion_as_the_clean_error(
    suggester: Suggester, fault: str
) -> None:
    clean = suggester.suggest(FAULTS[fault]["error"])
    dirty = suggester.suggest(noisy(fault))

    for lane in ("rca", "sop"):
        assert names(top(clean, lane)) == names(top(dirty, lane))


@pytest.mark.parametrize("text", [alert_text("disk_full"), noisy("disk_full")])
def test_issue_no_document_covers_says_nothing_relevant(suggester: Suggester, text: str) -> None:
    response = suggester.suggest(text)

    assert response.rca.nothing_relevant and response.sop.nothing_relevant
    assert "No past incident matches this issue." in response.message
    assert "No procedure covers this issue." in response.message


def test_message_names_an_alert_no_document_lists(suggester: Suggester) -> None:
    response = suggester.suggest(alert_text("disk_full"))

    assert response.issue.undocumented_alerts == ["OrdersApiDiskFull"]
    assert "No document lists the alert OrdersApiDiskFull." in response.message


def test_why_names_the_shared_alert_error_and_service(suggester: Suggester) -> None:
    response = suggester.suggest(alert_text("db_pool_exhausted"))
    rca, sop = top(response, "rca"), top(response, "sop")

    assert rca and rca.why == (
        "Past incident with the same alert OrdersDbPoolExhausted, error PoolTimeout"
        " and service orders-api."
    )
    assert sop and sop.why.startswith("Procedure with the same alert OrdersDbPoolExhausted")


def test_superseded_procedure_is_labelled_and_does_not_make_the_list_ambiguous(
    suggester: Suggester,
) -> None:
    lane = suggester.suggest(alert_text("bad_config_deploy")).sop

    assert [s.doc_id for s in lane.suggestions] == ["SOP-DEPLOY-002", "SOP-DEPLOY-001"]
    assert lane.suggestions[1].why.endswith("Superseded by SOP-DEPLOY-002.")
    assert not lane.ambiguous


def test_documents_sharing_only_words_are_dropped_when_others_share_the_alert(
    suggester: Suggester,
) -> None:
    lane = suggester.suggest(alert_text("bad_config_deploy")).rca

    assert [s.doc_id for s in lane.suggestions] == ["RCA-2026-007"]


@pytest.mark.parametrize("fault", FAULTS)
def test_lists_never_mix_rcas_and_sops(suggester: Suggester, fault: str) -> None:
    response = suggester.suggest(noisy(fault))

    assert all(s.doc_type == "rca" for s in response.rca.suggestions)
    assert all(s.doc_type == "sop" for s in response.sop.suggestions)


def test_suggest_p95_latency_is_under_500_ms(suggester: Suggester) -> None:
    suggester.suggest("warm up")

    latencies = [
        suggester.suggest(text).latency_ms
        for fault in FAULTS
        for text in (alert_text(fault), noisy(fault))
    ]

    assert statistics.quantiles(latencies, n=20)[18] < 500


def test_suggest_endpoint(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    settings = Settings(database_url=search_db_url, google_api_key="")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analyzer] = lambda: analyzer

    response = TestClient(app).post("/suggest", json={"text": noisy("db_pool_exhausted")})
    empty = TestClient(app).post("/suggest", json={"text": ""})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["sop"]["suggestions"][0]["doc_id"] == "SOP-DB-001"
    assert empty.status_code == 422
