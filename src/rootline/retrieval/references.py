import math
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from rootline.retrieval.search import STOPWORDS, WORD

# Embeddings put reference pages for a disk-full error no closer than a bread recipe (0.58
# against 0.57), so pages are matched on rare shared words instead. Measured on dev issues:
# the three disk pages score 5.3 to 7.2, unrelated pages 3.0 at most, the bread recipe 0.
MIN_SCORE = 4.0
# Pages far below the best one only share a word or two by chance.
RELATIVE = 0.5
LIMIT = 3

DOCS = "select count(distinct doc_id) from chunks"
# Prefix matching, so "connect" also counts "connection" the way a reader would.
FREQUENCY = """
    select t.term, count(distinct c.doc_id) as docs
    from unnest(%(terms)s::text[]) as t(term)
    join chunks c on c.tsv @@ to_tsquery('simple', t.term || ':*')
    group by t.term
"""
MATCHES = """
    select c.doc_id, array_agg(distinct t.term) as terms
    from unnest(%(terms)s::text[]) as t(term)
    join chunks c on c.doc_type = 'ref' and c.tsv @@ to_tsquery('simple', t.term || ':*')
    join documents d on d.doc_id = c.doc_id and d.status <> 'deleted'
    group by c.doc_id
"""
PAGES = """
    select d.doc_id, d.title, v.parsed -> 'fields' as fields
    from documents d
    join document_versions v on v.doc_id = d.doc_id and v.version = d.current_version
    where d.doc_id = any(%s)
"""


class Reference(BaseModel):
    doc_id: str
    title: str
    score: float
    matched: list[str]
    publisher: str
    source_url: str
    licence: str


def references(conn: psycopg.Connection[Any], text: str) -> list[Reference]:
    terms = [
        word
        for word in dict.fromkeys(WORD.findall(text.lower()))
        if len(word) >= 3 and word not in STOPWORDS and not word.isdigit()
    ]
    if not terms:
        return []
    with conn.cursor(row_factory=dict_row) as cursor:
        total = cursor.execute(DOCS).fetchone()
        frequency = {r["term"]: r["docs"] for r in cursor.execute(FREQUENCY, {"terms": terms})}
        matches = cursor.execute(MATCHES, {"terms": terms}).fetchall()
    docs = total["count"] if total else 0
    idf = {term: math.log((docs + 1) / (frequency.get(term, 0) + 1)) for term in terms}
    scored = sorted(
        (
            (sum(idf[t] for t in row["terms"]), row["doc_id"], sorted(row["terms"]))
            for row in matches
        ),
        reverse=True,
    )
    best = scored[0][0] if scored else 0.0
    kept = [s for s in scored if s[0] >= MIN_SCORE and s[0] >= RELATIVE * best][:LIMIT]
    if not kept:
        return []
    with conn.cursor(row_factory=dict_row) as cursor:
        pages = {r["doc_id"]: r for r in cursor.execute(PAGES, ([doc for _, doc, _ in kept],))}
    return [
        Reference(
            doc_id=doc_id,
            title=pages[doc_id]["title"],
            score=round(score, 2),
            matched=matched,
            publisher=str(pages[doc_id]["fields"].get("publisher", "")),
            source_url=str(pages[doc_id]["fields"].get("source_url", "")),
            licence=str(pages[doc_id]["fields"].get("licence", "")),
        )
        for score, doc_id, matched in kept
        if doc_id in pages
    ]
