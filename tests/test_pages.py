from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_agent_graph import ask, make_graph

from rootline.api.app import app, get_settings
from rootline.config import Settings
from rootline.ingest.analysis import Analyzer
from rootline.ingest.store import Store
from rootline.llm.ledger import Call, PostgresLedger

HTML = {"accept": "text/html"}


@pytest.fixture
def client(search_db_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=search_db_url, google_api_key="")
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_ops_shows_each_request_kind_with_its_cost_and_the_review_backlog(
    client: TestClient,
    search_db: psycopg.Connection,
    search_db_url: str,
    analyzer: Analyzer,
    offline_settings: Settings,
) -> None:
    state = ask(make_graph(analyzer, search_db_url, offline_settings), "steps for db pool")
    PostgresLedger(search_db_url).record(
        Call(
            request_id=state["request_id"],
            kind="llm_unavailable",
            task="answer_rca",
            model="test-model",
            prompt_version="v1",
            latency_ms=5,
            cost_usd=0.0123,
            degraded=("no_api_key",),
        )
    )
    search_db.execute(
        "insert into review_items (reason, details) values ('secrets_redacted', '{}')"
    )

    page = client.get("/ops")

    assert page.status_code == 200
    assert "<td>ask_sop</td>" in page.text and "0.0123" in page.text
    assert "no_api_key" in page.text and "<td>secrets_redacted</td>" in page.text
    row = search_db.execute(
        "select kind, path from requests where request_id = %s", (state["request_id"],)
    ).fetchone()
    assert row == ("ask_sop", "guard → understand → ask_sop")


def test_doc_page_has_section_anchors_and_api_clients_still_get_json(
    client: TestClient,
) -> None:
    page = client.get("/docs/RCA-2025-014", headers=HTML)
    data = client.get("/docs/RCA-2025-014")

    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert 'id="root-causes"' in page.text and 'href="#root-causes"' in page.text
    assert data.json()["doc_id"] == "RCA-2025-014"


def test_doc_page_never_shows_a_planted_secret(client: TestClient) -> None:
    page = client.get("/docs/SOP-B-A7F0F4", headers=HTML)

    assert page.status_code == 200
    assert "pk_canary_0c4a7e19b2d65f88" not in page.text
    assert "canary-tok-44f1e9" not in page.text


def test_fixing_the_type_moves_the_document_and_its_chunks(
    store: Store, migrated_db: psycopg.Connection, offline_settings: Settings
) -> None:
    source = Path("corpus/greenfield/rca-2025-022.md")
    store.ingest(str(source), source.read_bytes())
    row = migrated_db.execute(
        "insert into review_items (doc_id, reason, details) values ('RCA-2025-022',"
        " 'low_type_confidence', %s) returning id",
        (Jsonb({"label": "rca", "confidence": 0.55}),),
    ).fetchone()
    assert row
    app.dependency_overrides[get_settings] = lambda: offline_settings
    client = TestClient(app)

    page = client.get("/review")
    fixed = client.post(f"/review/{row[0]}/type", data={"doc_type": "sop"}, follow_redirects=False)
    again = client.post(f"/review/{row[0]}/type", data={"doc_type": "sop"}, follow_redirects=False)
    app.dependency_overrides.clear()

    assert "It is an SOP" in page.text and "low type confidence" in page.text
    assert fixed.status_code == 303 and again.status_code == 404
    doc_type = migrated_db.execute(
        "select doc_type from documents where doc_id = 'RCA-2025-022'"
    ).fetchone()
    chunk_types = migrated_db.execute(
        "select distinct doc_type from chunks where doc_id = 'RCA-2025-022'"
    ).fetchall()
    assert doc_type == ("sop",) and chunk_types == [("sop",)]
