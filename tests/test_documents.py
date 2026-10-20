import psycopg
from fastapi.testclient import TestClient

from rootline.api.app import app, get_settings
from rootline.config import Settings
from rootline.retrieval.documents import read_document


def test_sop_comes_with_its_stored_steps_in_order(search_db: psycopg.Connection) -> None:
    doc = read_document(search_db, "SOP-DB-001")
    count = search_db.execute(
        "select count(*) from sop_steps s join documents d on d.doc_id = s.doc_id"
        " and d.current_version = s.version where s.doc_id = 'SOP-DB-001'"
    ).fetchone()

    assert doc and doc.doc_type == "sop"
    assert [step.ordinal for step in doc.steps] == list(range(1, len(doc.steps) + 1))
    assert count and len(doc.steps) + len(doc.rollback) == count[0]


def test_rca_has_its_record_and_no_steps(search_db: psycopg.Connection) -> None:
    doc = read_document(search_db, "RCA-2025-014")

    assert doc and doc.doc_type == "rca" and doc.record
    assert doc.steps == doc.rollback == []


def test_docs_link_opens_the_document_and_unknown_ids_are_404(search_db_url: str) -> None:
    settings = Settings(database_url=search_db_url, google_api_key="")
    app.dependency_overrides[get_settings] = lambda: settings

    found = TestClient(app).get("/docs/sop-db-001")
    missing = TestClient(app).get("/docs/SOP-DB-999")
    app.dependency_overrides.clear()

    assert (found.status_code, found.json()["doc_id"]) == (200, "SOP-DB-001")
    assert missing.status_code == 404
