from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg

from rootline.core.context import request_scope
from rootline.ingest.store import Store

SOP = (Path(__file__).parents[1] / "corpus" / "greenfield" / "sop-cache-001.md").read_bytes()


def audit(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "select actor, action, source_key, doc_ids, request_id from audit order by id"
    ).fetchall()


def test_each_change_is_audited_with_its_actor_and_request(store: Store) -> None:
    store = replace(store, actor="api")

    with request_scope("req-7"):
        store.ingest("ops/sop.md", SOP)
        store.ingest("ops/sop.md", SOP)
        store.ingest("ops/copy.md", SOP)
        store.delete("ops/copy.md")

    assert audit(store.conn) == [
        ("api", "created", "ops/sop.md", ["SOP-CACHE-001"], "req-7"),
        ("api", "alias", "ops/copy.md", ["SOP-CACHE-001"], "req-7"),
        ("api", "deleted", "ops/copy.md", ["SOP-CACHE-001"], "req-7"),
    ]


def test_unchanged_upload_writes_no_audit_row(store: Store) -> None:
    store.ingest("ops/sop.md", SOP)
    store.ingest("ops/sop.md", SOP)

    assert [row[1] for row in audit(store.conn)] == ["created"]
