import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from rootline.api.app import app, get_analyzer, get_settings
from rootline.config import Settings
from rootline.incidents import AlertmanagerWebhook, Incidents
from rootline.ingest.analysis import Analyzer
from rootline.retrieval.search import Searcher
from rootline.retrieval.suggest import Suggester


def incidents_for(conn: psycopg.Connection, analyzer: Analyzer) -> Incidents:
    searcher = Searcher(conn, analyzer.embedder, analyzer.catalog.services)
    return Incidents(conn, Suggester(searcher, analyzer.catalog.alerts))


@pytest.fixture
def incidents(search_db: psycopg.Connection, analyzer: Analyzer) -> Incidents:
    return incidents_for(search_db, analyzer)


def webhook(status: str, fingerprint: str | None = None, **labels: str) -> AlertmanagerWebhook:
    alert: dict[str, Any] = {
        "status": status,
        "labels": {"alertname": "OrdersDbPoolExhausted", "service": "orders-api", **labels},
        "annotations": {
            "summary": "orders-api cannot get database connections",
            "description": "psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec",
        },
    }
    if fingerprint:
        alert["fingerprint"] = fingerprint
    return AlertmanagerWebhook.model_validate({"version": "4", "status": status, "alerts": [alert]})


def incident_row(conn: psycopg.Connection, incident_id: int | None) -> tuple[Any, ...]:
    row = conn.execute(
        "select status, fire_count, service, suggestions is not null from incidents where id = %s",
        (incident_id,),
    ).fetchone()
    assert row
    return row


def test_firing_alert_opens_an_incident_with_suggestions_and_a_thread(
    incidents: Incidents,
) -> None:
    [result] = incidents.receive(webhook("firing", uuid.uuid4().hex))

    assert result.action == "opened"
    assert result.thread_id and uuid.UUID(result.thread_id)
    assert result.suggestions
    assert result.suggestions.rca.suggestions[0].doc_id == "RCA-2025-014"
    assert result.suggestions.sop.suggestions[0].doc_id == "SOP-DB-001"
    assert incident_row(incidents.conn, result.incident_id) == ("open", 1, "orders-api", True)


def test_repeated_alert_reuses_the_open_incident(incidents: Incidents) -> None:
    fingerprint = uuid.uuid4().hex
    [first] = incidents.receive(webhook("firing", fingerprint))
    [again] = incidents.receive(webhook("firing", fingerprint))

    assert (again.action, again.incident_id, again.thread_id) == (
        "repeated",
        first.incident_id,
        first.thread_id,
    )
    assert again.suggestions == first.suggestions
    assert incident_row(incidents.conn, first.incident_id)[:2] == ("open", 2)


def test_resolved_alert_closes_the_incident_and_a_new_firing_opens_another(
    incidents: Incidents,
) -> None:
    fingerprint = uuid.uuid4().hex
    [opened] = incidents.receive(webhook("firing", fingerprint))
    [resolved] = incidents.receive(webhook("resolved", fingerprint))
    [reopened] = incidents.receive(webhook("firing", fingerprint))

    assert (resolved.action, resolved.incident_id) == ("resolved", opened.incident_id)
    assert incident_row(incidents.conn, opened.incident_id)[0] == "resolved"
    assert reopened.action == "opened"
    assert reopened.incident_id != opened.incident_id


def test_resolved_alert_without_an_open_incident_is_reported(incidents: Incidents) -> None:
    [result] = incidents.receive(webhook("resolved", uuid.uuid4().hex))

    assert (result.action, result.incident_id) == ("not_open", None)


def test_alert_without_a_fingerprint_is_keyed_by_its_labels(incidents: Incidents) -> None:
    pod = uuid.uuid4().hex
    [first] = incidents.receive(webhook("firing", pod=pod))
    [same] = incidents.receive(webhook("firing", pod=pod))
    [other] = incidents.receive(webhook("firing", pod=f"{pod}-2"))

    assert same.incident_id == first.incident_id
    assert other.incident_id != first.incident_id


def test_concurrent_alert_storm_opens_one_incident(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    fingerprint = uuid.uuid4().hex

    def send(_: int) -> str:
        with psycopg.connect(search_db_url, autocommit=True) as conn:
            return incidents_for(conn, analyzer).receive(webhook("firing", fingerprint))[0].action

    with ThreadPoolExecutor(max_workers=6) as pool:
        actions = list(pool.map(send, range(6)))

    rows = search_db.execute(
        "select count(*), max(fire_count) from incidents where fingerprint = %s", (fingerprint,)
    ).fetchone()
    assert sorted(actions) == ["opened"] + ["repeated"] * 5
    assert rows == (1, 6)


def test_incidents_endpoint_takes_an_alertmanager_webhook(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    settings = Settings(database_url=search_db_url, google_api_key="")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    payload = webhook("firing", uuid.uuid4().hex).model_dump()

    response = TestClient(app).post("/incidents", json={"receiver": "rootline", **payload})
    bad = TestClient(app).post("/incidents", json={"alerts": [{"status": "pending"}]})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["action"] == "opened"
    assert bad.status_code == 422
