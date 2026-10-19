import psycopg
import pytest

from rootline.db.migrate import migrate

ZERO_VECTOR = "[" + ",".join(["0"] * 384) + "]"


def add_document(conn: psycopg.Connection, doc_id: str, doc_type: str) -> None:
    conn.execute(
        "insert into documents (doc_id, source_key, doc_type, origin, title, confidence)"
        " values (%s, %s, %s, 'greenfield', 'title', 1.0)",
        (doc_id, f"test/{doc_id}.md", doc_type),
    )


def add_chunk(conn: psycopg.Connection, table: str, doc_id: str, doc_type: str) -> None:
    conn.execute(
        f"insert into {table} (chunk_id, doc_type, doc_id, version, section, ordinal, body,"
        " embedding, embed_model) values (%s, %s, %s, 1, 'summary', 0, 'text', %s, 'test')",
        (f"{doc_id}-0", doc_type, doc_id, ZERO_VECTOR),
    )


def test_migrate_creates_core_tables(empty_db: psycopg.Connection) -> None:
    migrate(empty_db)

    rows = empty_db.execute(
        "select table_name from information_schema.tables where table_schema = 'public'"
    ).fetchall()
    tables = {row[0] for row in rows}

    assert {
        "documents",
        "document_versions",
        "aliases",
        "chunks_rca",
        "chunks_sop",
        "chunks_ref",
        "sop_steps",
        "doc_links",
        "review_items",
        "incidents",
        "ledger",
    } <= tables


def test_migrate_twice_applies_nothing_new(empty_db: psycopg.Connection) -> None:
    migrate(empty_db)

    assert migrate(empty_db) == []


def test_chunk_lands_only_in_its_type_partition(empty_db: psycopg.Connection) -> None:
    migrate(empty_db)
    add_document(empty_db, "SOP-T-001", "sop")

    add_chunk(empty_db, "chunks", "SOP-T-001", "sop")

    assert empty_db.execute("select count(*) from chunks_sop").fetchone() == (1,)
    assert empty_db.execute("select count(*) from chunks_rca").fetchone() == (0,)


def test_rca_partition_rejects_sop_chunk(empty_db: psycopg.Connection) -> None:
    migrate(empty_db)
    add_document(empty_db, "SOP-T-001", "sop")

    with pytest.raises(psycopg.errors.CheckViolation):
        add_chunk(empty_db, "chunks_rca", "SOP-T-001", "sop")


def test_second_open_incident_with_same_fingerprint_is_rejected(
    empty_db: psycopg.Connection,
) -> None:
    migrate(empty_db)
    insert = "insert into incidents (fingerprint, payload) values ('fp-1', '{}')"
    empty_db.execute(insert)

    with pytest.raises(psycopg.errors.UniqueViolation):
        empty_db.execute(insert)
