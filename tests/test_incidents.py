import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from rootline.api.app import app, get_analyzer, get_settings
from rootline.config import Settings
from rootline.incidents import (
    AgentUnavailable,
    AlertmanagerWebhook,
    Incidents,
    open_chat,
    recent_incidents,
    thread_opener,
)
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


def test_alert_waits_in_the_list_until_someone_opens_it(incidents: Incidents) -> None:
    [result] = incidents.receive(webhook("firing", uuid.uuid4().hex))

    [listed] = [
        i for i in recent_incidents(incidents.conn, False, 100) if i.id == result.incident_id
    ]

    assert (listed.alert_name, listed.status, listed.chat_opened) == (
        "OrdersDbPoolExhausted",
        "open",
        False,
    )
    assert listed.top_rca and listed.top_rca.startswith("RCA-2025-014 · ")
    assert listed.top_sop and listed.top_sop.startswith("SOP-DB-001 · ")


def test_opening_an_alert_starts_its_chat_once(incidents: Incidents) -> None:
    opened: list[tuple[str, str]] = []
    [result] = incidents.receive(webhook("firing", uuid.uuid4().hex))
    assert result.incident_id

    first = open_chat(incidents.conn, result.incident_id, lambda *args: opened.append(args))
    second = open_chat(incidents.conn, result.incident_id, lambda *args: opened.append(args))

    assert first == second == result.thread_id
    assert len(opened) == 1 and opened[0][1].startswith("[FIRING:1] OrdersDbPoolExhausted")


def test_agent_server_down_leaves_the_alert_ready_for_another_try(incidents: Incidents) -> None:
    [result] = incidents.receive(webhook("firing", uuid.uuid4().hex))
    assert result.incident_id

    with pytest.raises(AgentUnavailable):
        open_chat(incidents.conn, result.incident_id, thread_opener("http://127.0.0.1:1"))
    opened = open_chat(incidents.conn, result.incident_id, lambda *args: None)

    assert opened == result.thread_id


def test_unknown_incident_cannot_be_opened(incidents: Incidents) -> None:
    assert open_chat(incidents.conn, 987654, lambda *args: None) is None


def test_alert_endpoints_list_open_and_allow_the_chat_ui(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    settings = Settings(
        database_url=search_db_url, google_api_key="", agent_url="http://127.0.0.1:1"
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    client = TestClient(app)
    client.post("/incidents", json=webhook("firing", uuid.uuid4().hex).model_dump())

    listed = client.get("/incidents")
    down = client.post(f"/incidents/{listed.json()[0]['id']}/open")
    missing = client.post("/incidents/987654/open")
    preflight = client.options(
        "/incidents",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    app.dependency_overrides.clear()

    assert listed.status_code == 200 and listed.json()[0]["status"] == "open"
    assert (down.status_code, missing.status_code) == (503, 404)
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:3000"
