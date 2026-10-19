import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from rootline.ingest.embedding import Embedder, vector_literal

Lane = Literal["rca", "sop"]
LANES: tuple[Lane, ...] = ("rca", "sop")

DOC_ID = re.compile(r"(?i)\b(?:SOP|RCA|REF)-(?:[A-Z0-9]+-\d{3}|B-[0-9A-F]{6})\b")
WORD = re.compile(r"[a-z0-9]+")
TOKEN = re.compile(r"[A-Za-z0-9_]+")
# Error names like OOMKilled, PoolTimeout or psycopg_pool are rare and specific.
IDENTIFIER = re.compile(r"[a-z][A-Z]|[A-Z]{2,}[a-z]|_|[A-Za-z]\d|\d[A-Za-z]")
# Words under 3 letters are dropped by length, so only longer function words are listed.
STOPWORDS = {
    "and", "are", "does", "for", "from", "how", "our", "please", "show", "that",
    "the", "this", "was", "were", "what", "when", "where", "which", "who", "why", "with", "you",
}  # fmt: skip
CANDIDATES = 30
RRF_K = 60
PER_LANE = 5
TITLE_MATCH = 0.6
TITLE_TIE = 0.1
SIMILARITY_TIE = 0.02
# Hits far below the best one in a list are nearby noise, not answers.
RELATIVE_GAP = 0.1
SNIPPET_CHARS = 220
# A superseded procedure stays findable, but the current one should come first.
SUPERSEDED_WEIGHT = 0.8
SERVICE_WEIGHT = 1.15
# Measured on dev queries: relevant results score 0.76 to 0.91, unrelated ones 0.50 to 0.67.
MIN_SIMILARITY = 0.72
# Accepted with less similarity when at least two words or a named service also match.
MIN_SIMILARITY_WITH_EVIDENCE = 0.62

DOCUMENTS = """
    select d.*,
        (select count(*) - 1 from documents m
         where m.cluster_id = d.cluster_id and m.status <> 'deleted') as copies,
        coalesce((select c.doc_id from documents c
                  where c.cluster_id = d.cluster_id and c.is_canonical and c.status <> 'deleted'
                  limit 1), d.doc_id) as canonical
    from documents d
    where d.doc_id = any(%s) and d.status <> 'deleted'
"""
LEXICAL = """
    select c.chunk_id, c.doc_id, c.section, c.body, d.services, d.status,
        ts_rank_cd(c.tsv, to_tsquery('english', %(q)s) || to_tsquery('simple', %(q)s)) as rank
    from chunks c join documents d on d.doc_id = c.doc_id
    where c.doc_type = %(t)s
      and c.tsv @@ (to_tsquery('english', %(q)s) || to_tsquery('simple', %(q)s))
    order by rank desc
    limit %(n)s
"""
SEMANTIC = """
    select c.chunk_id, c.doc_id, c.section, c.body, d.services, d.status,
        1 - (c.embedding <=> %(v)s::vector) as similarity
    from chunks c join documents d on d.doc_id = c.doc_id
    where c.doc_type = %(t)s
    order by c.embedding <=> %(v)s::vector
    limit %(n)s
"""
LINKS = """
    select l.from_doc as source, d.doc_id, d.doc_type, d.title, l.kind as relation
    from doc_links l join documents d on d.doc_id = l.to_doc
    where l.from_doc = any(%(ids)s) and d.status <> 'deleted' and d.is_canonical
    union
    select l.to_doc as source, d.doc_id, d.doc_type, d.title,
        case l.kind when 'supersedes' then 'superseded_by' else l.kind end as relation
    from doc_links l join documents d on d.doc_id = l.from_doc
    where l.to_doc = any(%(ids)s) and d.status <> 'deleted' and d.is_canonical
"""


class Linked(BaseModel):
    doc_id: str
    doc_type: str
    title: str
    relation: str


class Hit(BaseModel):
    doc_id: str
    doc_type: str
    title: str
    status: str
    origin: str
    verified: bool
    needs_review: bool
    effective_date: date | None
    services: list[str]
    score: float
    similarity: float
    evidence: list[str]
    snippet: str
    similar_copies: int
    linked: list[Linked]


class LaneResult(BaseModel):
    hits: list[Hit]
    ambiguous: bool = False
    nothing_relevant: bool = False


class SearchResponse(BaseModel):
    query: str
    mode: Literal["id", "title", "hybrid"]
    rca: LaneResult
    sop: LaneResult
    message: str = ""
    did_you_mean: list[Hit] = []
    latency_ms: float = 0.0


@dataclass(frozen=True)
class Match:
    score: float
    similarity: float
    evidence: list[str]
    snippet: str = ""


@dataclass(frozen=True)
class Searcher:
    conn: psycopg.Connection[Any]
    embedder: Embedder
    services: dict[str, list[str]]

    def search(self, query: str) -> SearchResponse:
        started = time.perf_counter()
        response = self.by_id(query) or self.by_title(query) or self.hybrid(query)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        return response.model_copy(update={"latency_ms": elapsed})

    def by_id(self, query: str) -> SearchResponse | None:
        match = DOC_ID.search(query)
        if not match:
            return None
        doc_id = match.group().upper()
        hits = self.hits({doc_id: Match(1.0, 1.0, [f"exact ID {doc_id}"])})
        if hits:
            return lanes_response(query, "id", hits)
        near = self.rows(
            "select doc_id from documents where status <> 'deleted'"
            " order by similarity(doc_id, %s) desc limit 3",
            (doc_id,),
        )
        label = ["similar ID, not the same document"]
        return SearchResponse(
            query=query,
            mode="id",
            rca=LaneResult(hits=[]),
            sop=LaneResult(hits=[]),
            message=f"No document has the ID {doc_id}.",
            did_you_mean=self.hits({r["doc_id"]: Match(0.0, 0.0, label) for r in near}),
        )

    def by_title(self, query: str) -> SearchResponse | None:
        rows = self.rows(
            "select doc_id, similarity(title, %(q)s) as score from documents"
            " where status <> 'deleted' and is_canonical and similarity(title, %(q)s) >= %(min)s"
            " order by score desc limit 5",
            {"q": query, "min": TITLE_MATCH},
        )
        if not rows:
            return None
        hits = self.hits(
            {
                r["doc_id"]: Match(r["score"], r["score"], [f"title match {r['score']:.2f}"])
                for r in rows
            }
        )
        close = [h for h in hits if hits[0].score - h.score < TITLE_TIE]
        if len(close) > 1:
            # Two titles fit almost equally well, so show both instead of picking one.
            return lanes_response(query, "title", close, ambiguous=True)
        # Other close titles stay visible, so a near-identical RCA is not hidden behind an SOP.
        response = lanes_response(query, "title", hits[:1])
        return response.model_copy(update={"did_you_mean": hits[1:]})

    def hybrid(self, query: str) -> SearchResponse:
        vector = vector_literal(self.embedder.query(query))
        words = dict.fromkeys(WORD.findall(query.lower()))
        terms = [w for w in words if len(w) >= 3 and w not in STOPWORDS]
        rare = {t.lower() for t in TOKEN.findall(query) if IDENTIFIER.search(t)}
        rare = {w for t in rare for w in WORD.findall(t)}
        evidence = Evidence(terms, rare, self.named_services(query))
        lanes = {lane: self.lane(lane, vector, evidence) for lane in LANES}
        return SearchResponse(query=query, mode="hybrid", rca=lanes["rca"], sop=lanes["sop"])

    def named_services(self, query: str) -> set[str]:
        text = query.lower()
        return {
            name
            for name, aliases in self.services.items()
            if any(
                re.search(rf"(?<![\w-]){re.escape(term.lower())}(?![\w-])", text)
                for term in [name, *aliases]
            )
        }

    def lane(self, lane: Lane, vector: str, evidence: "Evidence") -> LaneResult:
        semantic = self.rows(SEMANTIC, {"v": vector, "t": lane, "n": CANDIDATES})
        lexical: list[dict[str, Any]] = []
        if evidence.terms:
            query = " | ".join(evidence.terms)
            lexical = self.rows(LEXICAL, {"q": query, "t": lane, "n": CANDIDATES})

        # Reciprocal rank fusion: a chunk ranked well by either method rises; ranked well by
        # both, it rises most. It orders results but says nothing about absolute relevance.
        fused: dict[str, float] = defaultdict(float)
        chunks: dict[str, dict[str, Any]] = {}
        for ranked in (semantic, lexical):
            for rank, row in enumerate(ranked, start=1):
                fused[row["chunk_id"]] += 1 / (RRF_K + rank)
                chunks.setdefault(row["chunk_id"], row)
        similarity = {row["chunk_id"]: row["similarity"] for row in semantic}

        found: dict[str, Match] = {}
        for chunk_id, score in sorted(fused.items(), key=lambda item: item[1], reverse=True):
            row = chunks[chunk_id]
            if row["doc_id"] in found:
                continue
            match = judge(row, score, similarity.get(chunk_id, 0.0), evidence)
            if match:
                found[row["doc_id"]] = match

        hits = self.hits(found)
        if not hits:
            return LaneResult(hits=[], nothing_relevant=True)
        best = max(hit.similarity for hit in hits)
        hits = [hit for hit in hits if hit.similarity >= best - RELATIVE_GAP]
        # Current documents first; a superseded one stays visible after them.
        hits = sorted(hits, key=lambda hit: hit.status == "superseded")[:PER_LANE]
        tied = len(hits) > 1 and abs(hits[0].similarity - hits[1].similarity) < SIMILARITY_TIE
        return LaneResult(hits=hits, ambiguous=tied)

    def hits(self, found: dict[str, Match]) -> list[Hit]:
        docs = self.documents(list(found))
        # Brownfield copies collapse into their canonical document, keeping the best match.
        merged: dict[str, Match] = {}
        for doc_id, match in found.items():
            if doc_id not in docs:
                continue
            target = docs[doc_id]["canonical"]
            if target not in merged or merged[target].score < match.score:
                merged[target] = match
        docs |= self.documents([d for d in merged if d not in docs])
        links = self.links(list(merged))
        hits = [
            Hit(
                doc_id=doc_id,
                doc_type=docs[doc_id]["doc_type"],
                title=docs[doc_id]["title"],
                status=docs[doc_id]["status"],
                origin=docs[doc_id]["origin"],
                verified=docs[doc_id]["verified"],
                needs_review=docs[doc_id]["needs_review"],
                effective_date=docs[doc_id]["effective_date"],
                services=docs[doc_id]["services"],
                score=round(match.score, 5),
                similarity=round(match.similarity, 3),
                evidence=match.evidence,
                snippet=match.snippet,
                similar_copies=max(docs[doc_id]["copies"] or 0, 0),
                linked=links.get(doc_id, []),
            )
            for doc_id, match in merged.items()
        ]
        return sorted(hits, key=lambda hit: hit.score, reverse=True)

    def documents(self, doc_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not doc_ids:
            return {}
        return {row["doc_id"]: row for row in self.rows(DOCUMENTS, (doc_ids,))}

    def links(self, doc_ids: list[str]) -> dict[str, list[Linked]]:
        linked: dict[str, list[Linked]] = defaultdict(list)
        for row in self.rows(LINKS, {"ids": doc_ids}):
            if row["doc_id"] != row["source"]:
                linked[row["source"]].append(Linked.model_validate(row))
        return linked

    def rows(self, sql: str, params: Any) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


@dataclass(frozen=True)
class Evidence:
    terms: list[str]
    rare: set[str]
    services: set[str]


def judge(row: dict[str, Any], score: float, similarity: float, evidence: Evidence) -> Match | None:
    body = row["body"].lower()
    matched = [t for t in evidence.terms if re.search(rf"\b{re.escape(t)}", body)]
    services = sorted(evidence.services & set(row["services"]))
    # A rare identifier counts twice: matching "OOMKilled" says more than matching "orders".
    support = len(matched) + len(evidence.rare & set(matched)) + bool(services)
    if similarity < MIN_SIMILARITY and not (
        support >= 2 and similarity >= MIN_SIMILARITY_WITH_EVIDENCE
    ):
        return None
    if services:
        score *= SERVICE_WEIGHT
    if row["status"] == "superseded":
        score *= SUPERSEDED_WEIGHT
    reasons = [f"section: {row['section']}", f"similarity {similarity:.2f}"]
    if services:
        reasons.insert(0, f"service: {', '.join(services)}")
    if matched:
        reasons.insert(0, f"matched: {', '.join(matched[:6])}")
    return Match(score, similarity, reasons, snippet(row["body"]))


def lanes_response(
    query: str, mode: Literal["id", "title"], hits: list[Hit], ambiguous: bool = False
) -> SearchResponse:
    lanes = {
        lane: LaneResult(hits=[h for h in hits if h.doc_type == lane], ambiguous=ambiguous)
        for lane in LANES
    }
    return SearchResponse(query=query, mode=mode, rca=lanes["rca"], sop=lanes["sop"])


def snippet(body: str) -> str:
    text = body.split("\n", 1)[-1].replace("\n", " ")
    return text[:SNIPPET_CHARS] + ("..." if len(text) > SNIPPET_CHARS else "")
