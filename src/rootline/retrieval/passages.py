from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from rootline.ingest.embedding import Embedder, vector_literal

# The card chunk is a summary built at ingest; answers cite the document's own sections.
PASSAGES = """
    select c.chunk_id, c.doc_id, d.title, c.section, c.body,
        1 - (c.embedding <=> %(v)s::vector) as similarity
    from chunks c join documents d on d.doc_id = c.doc_id
    where c.doc_type = %(t)s and c.doc_id = any(%(ids)s) and c.section <> 'card'
    order by c.embedding <=> %(v)s::vector
    limit %(n)s
"""


class Passage(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    section: str
    body: str
    similarity: float

    @property
    def text(self) -> str:
        # Section chunks start with a "[RCA · ID · services] section" header line.
        return self.body.split("\n", 1)[-1]


def top_passages(
    conn: psycopg.Connection[Any],
    embedder: Embedder,
    question: str,
    doc_ids: list[str],
    doc_type: str,
    limit: int,
) -> list[Passage]:
    if not doc_ids:
        return []
    params = {
        "v": vector_literal(embedder.query(question)),
        "t": doc_type,
        "ids": doc_ids,
        "n": limit,
    }
    with conn.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(PASSAGES, params).fetchall()
    return [Passage.model_validate(row) for row in rows]
