import json
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from rootline.api.app import app, get_analyzer, get_settings
from rootline.config import Settings
from rootline.ingest.analysis import Analyzer
from rootline.ingest.factory import ingest_path
from rootline.ingest.store import Outcome, Store

CORPUS = Path(__file__).parents[1] / "corpus"


def ingest(store: Store, relative: str) -> Outcome:
    return store.ingest(f"corpus/{relative}", (CORPUS / relative).read_bytes())


def one(conn: psycopg.Connection, sql: str, params: tuple[object, ...] = ()) -> object:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def test_exact_copy_under_a_new_name_becomes_an_alias(store: Store) -> None:
    ingest(store, "greenfield/sop-cache-001.md")

    outcome = ingest(store, "brownfield/cache-down-runbook-copy.md")

    assert (outcome.action, outcome.doc_ids) == ("alias", ["SOP-CACHE-001"])
    assert one(store.conn, "select count(*) from documents") == 1


def test_same_text_in_different_bytes_becomes_an_alias(store: Store) -> None:
    text = (CORPUS / "greenfield" / "sop-cache-001.md").read_text()
    store.ingest("a/sop-cache.md", text.encode())

    outcome = store.ingest("b/sop-cache.md", text.replace("\n", "\r\n").encode())

    assert outcome.action == "alias"
    assert one(store.conn, "select reason from aliases") == "same_content"


def test_deleting_the_original_keeps_the_document_under_its_copy(store: Store) -> None:
    ingest(store, "greenfield/sop-cache-001.md")
    ingest(store, "brownfield/cache-down-runbook-copy.md")

    store.delete("corpus/greenfield/sop-cache-001.md")

    assert one(store.conn, "select status from documents") == "active"
    assert one(store.conn, "select source_key from documents") == (
        "corpus/brownfield/cache-down-runbook-copy.md"
    )


def test_declared_id_under_a_new_file_name_is_a_new_version(store: Store) -> None:
    original = (CORPUS / "greenfield" / "sop-cache-001.md").read_text()
    store.ingest("old-name.md", original.encode())

    store.ingest("new-name.md", original.replace("Restart the cache.", "Restart it.").encode())

    assert one(store.conn, "select count(*) from documents") == 1
    assert one(store.conn, "select current_version from documents") == 2


def test_mixed_file_becomes_a_linked_rca_and_sop(store: Store) -> None:
    outcome = ingest(store, "brownfield/orders-worker-oom-runbook.html")

    types = store.conn.execute("select doc_type from documents order by doc_type").fetchall()
    assert [row[0] for row in types] == ["rca", "sop"]
    assert len(outcome.doc_ids) == 2
    assert one(store.conn, "select count(*) from doc_links where kind = 'split_from'") == 2


def test_newer_sop_marks_the_older_one_superseded(store: Store) -> None:
    ingest(store, "greenfield/sop-queue-002.docx")
    ingest(store, "greenfield/sop-queue-001.pdf")

    status = "select status from documents where doc_id = %s"
    assert one(store.conn, status, ("SOP-QUEUE-001",)) == "superseded"
    assert one(store.conn, status, ("SOP-QUEUE-002",)) == "active"


def test_brownfield_copy_joins_the_greenfield_cluster_as_non_canonical(store: Store) -> None:
    ingest(store, "greenfield/sop-cache-001.md")

    copy = ingest(store, "brownfield/postmortem-cache-outage-notes.txt")

    cluster = "select cluster_id, is_canonical from documents where doc_id = %s"
    assert store.conn.execute(cluster, (copy.doc_ids[0],)).fetchone() == ("SOP-CACHE-001", False)
    assert store.conn.execute(cluster, ("SOP-CACHE-001",)).fetchone() == ("SOP-CACHE-001", True)


def test_secrets_never_reach_chunks_and_are_raised_for_review(store: Store) -> None:
    ingest(store, "brownfield/re-checkout-401s-tonight.eml")

    canary = "pk_canary_5d2e9b7f1a4c8e3b"
    assert one(store.conn, "select count(*) from chunks where body like %s", (f"%{canary}%",)) == 0
    details = one(store.conn, "select details from review_items where reason = 'secrets_redacted'")
    assert details == {"API_KEY": 1}


def test_unclassifiable_file_goes_to_review_without_a_document(store: Store) -> None:
    outcome = store.ingest("notes/hello.txt", b"hello there\n\nsee you at lunch tomorrow\n")

    assert outcome.action == "review_only"
    assert one(store.conn, "select count(*) from documents") == 0
    assert one(store.conn, "select reason from review_items") == "unknown_type"


def test_sync_deletes_documents_whose_file_was_removed(store: Store, tmp_path: Path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    for name in ("sop-cache-001.md", "rca-2025-031.md"):
        (folder / name).write_bytes((CORPUS / "greenfield" / name).read_bytes())
    ingest_path(store, folder, sync=True)

    (folder / "rca-2025-031.md").unlink()
    outcomes = ingest_path(store, folder, sync=True)

    assert [o.action for o in outcomes] == ["unchanged", "deleted"]
    assert (
        one(store.conn, "select status from documents where doc_id = 'RCA-2025-031'") == "deleted"
    )


def test_api_uploads_and_deletes_a_document(
    migrated_db: psycopg.Connection, analyzer: Analyzer, offline_settings: Settings, tmp_path: Path
) -> None:
    settings = offline_settings.model_copy(update={"raw_dir": tmp_path / "raw"})
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    client = TestClient(app)
    content = (CORPUS / "greenfield" / "sop-cache-001.md").read_bytes()

    uploaded = client.post(
        "/documents", files={"file": ("sop.md", content)}, data={"source_key": "api/sop.md"}
    )
    deleted = client.delete("/documents", params={"source_key": "api/sop.md"})
    missing = client.delete("/documents", params={"source_key": "api/sop.md"})
    unsupported = client.post("/documents", files={"file": ("notes.xyz", b"data")})
    app.dependency_overrides.clear()

    assert (uploaded.status_code, uploaded.json()["action"]) == (200, "created")
    assert json.loads(deleted.text)["action"] == "deleted"
    assert (missing.status_code, unsupported.status_code) == (404, 415)
