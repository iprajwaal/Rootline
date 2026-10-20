import logging
from collections import defaultdict
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from rootline.ingest.embedding import Embedder, EmbeddingUnavailable, vector_literal
from rootline.retrieval.search import RRF_K, STOPWORDS, WORD

log = logging.getLogger(__name__)

# The card chunk is a summary built at ingest; answers cite the document's own sections.
PASSAGES = """
    select c.chunk_id, c.doc_id, d.title, c.section, c.body,
        1 - (c.embedding <=> %(v)s::vector) as similarity
    from chunks c join documents d on d.doc_id = c.doc_id
    where c.doc_type = %(t)s and c.doc_id = any(%(ids)s) and c.section <> 'card'
    order by c.embedding <=> %(v)s::vector
    limit %(n)s
"""
PASSAGES_BY_WORDS = """
    select c.chunk_id, c.doc_id, d.title, c.section, c.body, 0.0 as similarity
    from chunks c join documents d on d.doc_id = c.doc_id
    where c.doc_type = %(t)s and c.doc_id = any(%(ids)s) and c.section <> 'card'
        and c.tsv @@ websearch_to_tsquery('english', %(q)s)
    order by ts_rank(c.tsv, websearch_to_tsquery('english', %(q)s)) desc
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
        # Section chunks start with a "[RCA · ID vN · services] section" header line.
        return self.body.split("\n", 1)[-1]


def top_passages(
    conn: psycopg.Connection[Any],
    embedder: Embedder,
    question: str,
    doc_ids: list[str],
    doc_type: str,
    limit: int,
) -> list[Passage]:
    # Meaning and the question's words are ranked apart and fused, as in search, so a section
    # that names the error is kept even when timeline rows read closer in meaning.
    if not doc_ids:
        return []
    words = [w for w in dict.fromkeys(WORD.findall(question.lower())) if len(w) >= 3]
    words = [w for w in words if w not in STOPWORDS]
    params = {"q": " or ".join(words), "t": doc_type, "ids": doc_ids, "n": limit * 3}
    rankings = []
    with conn.cursor(row_factory=dict_row) as cursor:
        try:
            params["v"] = vector_literal(embedder.query(question))
            rankings.append(cursor.execute(PASSAGES, params).fetchall())
        except EmbeddingUnavailable as error:
            log.warning("passages ranked by words only error=%s", error)
        if words:
            rankings.append(cursor.execute(PASSAGES_BY_WORDS, params).fetchall())
    fused: dict[str, float] = defaultdict(float)
    rows: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, start=1):
            fused[row["chunk_id"]] += 1 / (RRF_K + rank)
            rows.setdefault(row["chunk_id"], row)
    best = sorted(fused, key=lambda chunk_id: fused[chunk_id], reverse=True)[:limit]
    return [Passage.model_validate(rows[chunk_id]) for chunk_id in best]
