from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from rootline.ingest.store import Outcome, Store

DRAFTS = """
    select id, source_key, status, details, created_at, resolved_at from review_items
    where reason = 'draft' and (status = 'open' or resolved_at > now() - interval '1 day')
    order by status = 'open' desc, id desc
"""
ITEMS = """
    select id, reason, doc_id, source_key, details, created_at from review_items
    where reason <> 'draft' and status = 'open' order by created_at, id
"""
# Only an open item is claimed, so a second click changes nothing.
RESOLVE = """
    update review_items set status = 'approved', resolved_at = now()
    where id = %s and reason <> 'draft' and status = 'open'
    returning id
"""
CLAIM_TYPE = """
    update review_items set status = 'approved', resolved_at = now()
    where id = %s and reason = any(%s) and doc_id is not null and status = 'open'
    returning doc_id
"""
RETYPE = [
    "update documents set doc_type = %(t)s, needs_review = false, updated_at = now()"
    " where doc_id = %(d)s",
    # The partition key changes, so each chunk moves to the new type's partition.
    "update chunks set doc_type = %(t)s,"
    " body = regexp_replace(body, '^\\[(RCA|SOP|REF)', '[' || upper(%(t)s))"
    " where doc_id = %(d)s",
    "delete from sop_steps where doc_id = %(d)s and %(t)s <> 'sop'",
]
# The classifier was unsure of these, so a person settles the type.
TYPE_REASONS = ("low_type_confidence",)
# Only an open draft can be claimed, so a double click never ingests it twice.
CLAIM = """
    update review_items set status = %s, resolved_at = now()
    where id = %s and reason = 'draft' and status = 'open'
    returning source_key, details
"""


class Draft(BaseModel):
    id: int
    status: str
    doc_id: str
    doc_type: str
    title: str
    markdown: str
    source_key: str
    created_at: datetime


def drafts(conn: psycopg.Connection[Any]) -> list[Draft]:
    with conn.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(DRAFTS).fetchall()
    return [Draft.model_validate(row | row.pop("details")) for row in rows]


class Item(BaseModel):
    id: int
    reason: str
    doc_id: str | None
    source_key: str | None
    details: dict[str, Any]
    created_at: datetime

    @property
    def retypable(self) -> bool:
        return self.reason in TYPE_REASONS and self.doc_id is not None


def open_items(conn: psycopg.Connection[Any]) -> list[Item]:
    with conn.cursor(row_factory=dict_row) as cursor:
        return [Item.model_validate(row) for row in cursor.execute(ITEMS).fetchall()]


def mark_reviewed(conn: psycopg.Connection[Any], review_id: int) -> bool:
    return conn.execute(RESOLVE, (review_id,)).fetchone() is not None


def fix_type(conn: psycopg.Connection[Any], review_id: int, doc_type: str) -> bool:
    # One transaction, so a failed move leaves the item open to try again.
    with conn.transaction():
        claimed = conn.execute(CLAIM_TYPE, (review_id, list(TYPE_REASONS))).fetchone()
        if claimed is None:
            return False
        for sql in RETYPE:
            conn.execute(sql, {"t": doc_type, "d": claimed[0]})
    return True


def approve(conn: psycopg.Connection[Any], store: Store, review_id: int) -> Outcome | None:
    with conn.cursor(row_factory=dict_row) as cursor:
        claimed = cursor.execute(CLAIM, ("approved", review_id)).fetchone()
    if claimed is None:
        return None
    try:
        return store.ingest(claimed["source_key"], claimed["details"]["markdown"].encode())
    except Exception:
        # The draft goes back to the queue, so a failed ingest can be tried again.
        conn.execute(
            "update review_items set status = 'open', resolved_at = null where id = %s",
            (review_id,),
        )
        raise


def reject(conn: psycopg.Connection[Any], review_id: int) -> bool:
    # A rejected draft stays as a record of the incident but is never indexed.
    return conn.execute(CLAIM, ("rejected", review_id)).fetchone() is not None
