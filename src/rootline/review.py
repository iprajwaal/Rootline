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
OTHERS = """
    select reason, count(*) as items from review_items
    where reason <> 'draft' and status = 'open' group by reason order by reason
"""
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


def other_reviews(conn: psycopg.Connection[Any]) -> dict[str, int]:
    return {row[0]: row[1] for row in conn.execute(OTHERS)}


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
