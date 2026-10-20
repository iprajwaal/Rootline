import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row

from rootline.core.context import REQUEST_ID
from rootline.core.models import SopRecord
from rootline.ingest.analysis import Analysis, Analyzer, Part
from rootline.ingest.chunking import stamp
from rootline.ingest.classify import needs_review
from rootline.ingest.duplicates import (
    DUPLICATE_AT,
    POSSIBLE_DUPLICATE_AT,
    boilerplate,
    containment,
)
from rootline.ingest.embedding import vector_literal
from rootline.ingest.hashing import raw_sha256, store_raw

Action = Literal["unchanged", "alias", "created", "updated", "review_only", "deleted"]
CLUSTER_CANDIDATES = 10
BOILERPLATE_SAMPLE = 1000

CURRENT_DOCS = """
    select d.doc_id, d.source_key, d.current_version, d.cluster_id, v.raw_sha256, v.content_hash
    from documents d
    join document_versions v on v.doc_id = d.doc_id and v.version = d.current_version
    where d.status <> 'deleted'
      and (d.source_key = %(key)s or left(d.source_key, length(%(key)s) + 1) = %(key)s || '#')
"""


@dataclass(frozen=True)
class Outcome:
    source_key: str
    action: Action
    doc_ids: list[str]


@dataclass(frozen=True)
class Store:
    conn: psycopg.Connection[Any]
    analyzer: Analyzer
    raw_dir: Path
    embed_model: str
    actor: str = "system"

    def ingest(self, source_key: str, data: bytes) -> Outcome:
        raw = raw_sha256(data)
        # Cheap check first, so an unchanged file never pays for parsing, the LLM or embedding.
        if unchanged := self.unchanged(source_key, raw):
            return unchanged
        store_raw(data, self.raw_dir)
        analysis = self.analyzer.analyse(source_key, data)

        with self.conn.transaction():
            for key in (f"source:{source_key}", f"raw:{raw}", f"content:{analysis.content_hash}"):
                self.run("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
            # Another process may have finished the same file while this one was analysing.
            if unchanged := self.unchanged(source_key, raw):
                return unchanged
            current = self.rows(CURRENT_DOCS, {"key": source_key})
            if not current and (twin := self.twin(raw, analysis.content_hash)):
                doc_id, reason = twin
                self.run(
                    "insert into aliases (source_key, doc_id, reason, raw_sha256)"
                    " values (%s, %s, %s, %s)"
                    " on conflict (source_key) do update set doc_id = excluded.doc_id,"
                    " reason = excluded.reason, raw_sha256 = excluded.raw_sha256",
                    (source_key, doc_id, reason, raw),
                )
                return self.audited(Outcome(source_key, "alias", [doc_id]))
            if not analysis.parts:
                self.review(None, source_key, "unknown_type", analysis.classification.model_dump())
                return self.audited(Outcome(source_key, "review_only", []))

            doc_ids = [self.write_part(part, analysis, current) for part in analysis.parts]
            written = {part.key for part in analysis.parts}
            for row in current:
                if row["source_key"] not in written:
                    self.tombstone(row["doc_id"])
            if len(doc_ids) == 2:
                self.link(doc_ids[0], doc_ids[1], "split_from")
                self.link(doc_ids[1], doc_ids[0], "split_from")
            self.refresh_superseded()
            return self.audited(Outcome(source_key, "updated" if current else "created", doc_ids))

    def delete(self, source_key: str) -> Outcome:
        with self.conn.transaction():
            self.run(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"source:{source_key}",)
            )
            removed = self.rows(
                "delete from aliases where source_key = %s returning doc_id", (source_key,)
            )
            if removed:
                return self.audited(Outcome(source_key, "deleted", [removed[0]["doc_id"]]))
            current = self.rows(CURRENT_DOCS, {"key": source_key})
            for row in current:
                successor = self.rows(
                    "delete from aliases where source_key = ("
                    "  select source_key from aliases where doc_id = %s order by source_key limit 1"
                    ") returning source_key",
                    (row["doc_id"],),
                )
                if successor:
                    # The same text still exists under another name, so the document stays.
                    suffix = row["source_key"][len(source_key) :]
                    self.run(
                        "update documents set source_key = %s, updated_at = now()"
                        " where doc_id = %s",
                        (successor[0]["source_key"] + suffix, row["doc_id"]),
                    )
                else:
                    self.tombstone(row["doc_id"])
            self.refresh_superseded()
            return self.audited(Outcome(source_key, "deleted", [row["doc_id"] for row in current]))

    def sync_removed(self, prefix: str, seen: set[str]) -> list[Outcome]:
        keys = {
            row["source_key"].split("#")[0]
            for row in self.rows(
                "select source_key from documents where status <> 'deleted'"
                " and left(source_key, length(%(p)s)) = %(p)s"
                " union select source_key from aliases"
                " where left(source_key, length(%(p)s)) = %(p)s",
                {"p": prefix},
            )
        }
        return [self.delete(key) for key in sorted(keys - seen)]

    def audited(self, outcome: Outcome) -> Outcome:
        self.run(
            "insert into audit (actor, action, source_key, doc_ids, request_id)"
            " values (%s, %s, %s, %s, %s)",
            (self.actor, outcome.action, outcome.source_key, outcome.doc_ids, REQUEST_ID.get()),
        )
        return outcome

    def unchanged(self, source_key: str, raw: str) -> Outcome | None:
        current = self.rows(CURRENT_DOCS, {"key": source_key})
        if current and all(row["raw_sha256"] == raw for row in current):
            return Outcome(source_key, "unchanged", [row["doc_id"] for row in current])
        alias = self.rows(
            "select a.doc_id from aliases a join documents d on d.doc_id = a.doc_id"
            " where a.source_key = %s and a.raw_sha256 = %s and d.status <> 'deleted'",
            (source_key, raw),
        )
        return Outcome(source_key, "unchanged", [alias[0]["doc_id"]]) if alias else None

    def twin(self, raw: str, content: str) -> tuple[str, str] | None:
        for column, reason in (("raw_sha256", "same_raw"), ("content_hash", "same_content")):
            rows = self.rows(
                "select d.doc_id from documents d"
                " join document_versions v on v.doc_id = d.doc_id and v.version = d.current_version"
                f" where d.status <> 'deleted' and v.{column} = %s order by d.doc_id limit 1",
                (raw if column == "raw_sha256" else content,),
            )
            if rows:
                return rows[0]["doc_id"], reason
        return None

    def write_part(self, part: Part, analysis: Analysis, current: list[dict[str, Any]]) -> str:
        part_hash = hashlib.sha256(part.doc.text.encode()).hexdigest()
        existing = next((r for r in current if r["source_key"] == part.key), None)
        if existing is None:
            # A declared ID is the document's identity, whatever file name it arrives under.
            existing = next(
                iter(
                    self.rows(
                        "select doc_id, current_version from documents where doc_id = %s",
                        (part.doc_id,),
                    )
                ),
                None,
            )
        doc_id = existing["doc_id"] if existing else part.doc_id

        known = self.rows(
            "select version from document_versions where doc_id = %s and content_hash = %s",
            (doc_id, part_hash),
        )
        if known:
            version = known[0]["version"]
        else:
            version = (existing["current_version"] if existing else 0) + 1
        review = needs_review(part.classification) or not part.extraction.verified
        values = {
            "doc_id": doc_id,
            "source_key": part.key,
            "doc_type": part.label,
            "origin": part.origin,
            "title": part.doc.title,
            "services": part.facts.services,
            "alerts": part.facts.alerts,
            "version": version,
            "confidence": part.classification.confidence,
            "needs_review": review,
            "verified": part.extraction.verified,
            "effective_date": as_date(part.facts.effective_date),
        }
        self.run(
            "insert into documents (doc_id, source_key, doc_type, origin, title, services, alerts,"
            " current_version, confidence, needs_review, verified, effective_date)"
            " values (%(doc_id)s, %(source_key)s, %(doc_type)s, %(origin)s, %(title)s,"
            " %(services)s, %(alerts)s, %(version)s, %(confidence)s, %(needs_review)s,"
            " %(verified)s, %(effective_date)s)"
            " on conflict (doc_id) do update set doc_type = excluded.doc_type,"
            " origin = excluded.origin, title = excluded.title, services = excluded.services,"
            " alerts = excluded.alerts, current_version = excluded.current_version,"
            " confidence = excluded.confidence, needs_review = excluded.needs_review,"
            " verified = excluded.verified, effective_date = excluded.effective_date,"
            " status = 'active', updated_at = now()",
            values,
        )
        if existing and existing.get("source_key") not in (None, part.key):
            self.run(
                "insert into aliases (source_key, doc_id, reason, raw_sha256)"
                " values (%s, %s, 'same_content', %s) on conflict (source_key) do nothing",
                (part.key, doc_id, analysis.raw_sha),
            )
        if not known:
            self.insert_version(part, doc_id, version, analysis.raw_sha, part_hash)
        self.replace_chunks(part, doc_id, version)
        self.replace_steps(part, doc_id, version)
        self.replace_links(part, doc_id)
        self.replace_reviews(part, doc_id, analysis)
        self.cluster(part, doc_id)
        return doc_id

    def insert_version(
        self, part: Part, doc_id: str, version: int, raw: str, part_hash: str
    ) -> None:
        record = part.extraction.record.model_dump() if part.extraction.record else {}
        self.run(
            "insert into document_versions (doc_id, version, raw_sha256, content_hash, parsed,"
            " record, extraction_confidence, facts, classification, extraction_method)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                doc_id,
                version,
                raw,
                part_hash,
                json.dumps(part.doc.model_dump()),
                json.dumps(record),
                1.0 if part.extraction.verified else 0.0,
                json.dumps(part.facts.model_dump()),
                json.dumps(part.classification.model_dump()),
                part.extraction.method,
            ),
        )

    def replace_chunks(self, part: Part, doc_id: str, version: int) -> None:
        self.run("delete from chunks where doc_id = %s", (doc_id,))
        with self.conn.cursor() as cursor:
            cursor.executemany(
                "insert into chunks (chunk_id, doc_type, doc_id, version, section, ordinal, body,"
                " embedding, embed_model) values (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
                [
                    (
                        f"{doc_id}:{version}:{chunk.ordinal}",
                        part.label,
                        doc_id,
                        version,
                        chunk.section,
                        chunk.ordinal,
                        stamp(chunk.body, doc_id, version),
                        vector_literal(vector),
                        self.embed_model,
                    )
                    for chunk, vector in zip(part.chunks, part.vectors, strict=True)
                ],
            )

    def replace_steps(self, part: Part, doc_id: str, version: int) -> None:
        self.run("delete from sop_steps where doc_id = %s", (doc_id,))
        record = part.extraction.record
        if not isinstance(record, SopRecord):
            return
        with self.conn.cursor() as cursor:
            cursor.executemany(
                "insert into sop_steps (doc_id, version, section, ordinal, instruction, command,"
                " action, check_name, expect, source_start, source_end)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        doc_id,
                        version,
                        section,
                        step.ordinal,
                        step.instruction,
                        step.command,
                        step.action,
                        step.check_name,
                        step.expect,
                        step.source_start,
                        step.source_end,
                    )
                    for section, steps in (("steps", record.steps), ("rollback", record.rollback))
                    for step in steps
                ],
            )

    def replace_links(self, part: Part, doc_id: str) -> None:
        self.run("delete from doc_links where from_doc = %s and kind <> 'split_from'", (doc_id,))
        for mentioned in part.facts.mentions:
            self.link(doc_id, mentioned, "related")
        if part.facts.supersedes:
            self.link(doc_id, part.facts.supersedes, "supersedes")

    def replace_reviews(self, part: Part, doc_id: str, analysis: Analysis) -> None:
        self.run("delete from review_items where doc_id = %s and status = 'open'", (doc_id,))
        if needs_review(part.classification):
            self.review(doc_id, part.key, "low_type_confidence", part.classification.model_dump())
        if not part.extraction.verified:
            self.review(
                doc_id, part.key, "extraction_unverified", {"issues": part.extraction.issues}
            )
        if analysis.secret_kinds:
            counts = {
                kind: analysis.secret_kinds.count(kind) for kind in set(analysis.secret_kinds)
            }
            self.review(doc_id, part.key, "secrets_redacted", counts)

    def cluster(self, part: Part, doc_id: str) -> None:
        # Embeddings pick candidates on the same topic; shared wording decides if it is a copy.
        # Two templated documents with their own IDs are separate documents, even when the
        # template wording makes them look alike; a newer one replaces an older one through
        # "supersedes", not through clustering.
        candidates = self.rows(
            "select d.doc_id, d.cluster_id, v.parsed ->> 'text' as text from chunks c"
            " join documents d on d.doc_id = c.doc_id"
            " join document_versions v on v.doc_id = d.doc_id and v.version = d.current_version"
            " where c.doc_type = %s and c.section = 'card' and d.status <> 'deleted'"
            " and d.doc_id <> %s and not (d.origin = 'greenfield' and %s = 'greenfield')"
            " order by c.embedding <=> %s::vector limit %s",
            (part.label, doc_id, part.origin, vector_literal(part.vectors[0]), CLUSTER_CANDIDATES),
        )
        # Copies of one document share a cluster, so they count once: boilerplate is wording
        # shared by many different documents, not by many copies of the same one.
        groups: dict[str, str] = {}
        for row in self.same_type_texts(part.label):
            groups[row["group_key"]] = groups.get(row["group_key"], "") + "\n" + row["text"]
        ignore = boilerplate(list(groups.values()))
        scored = sorted(
            ((containment(part.doc.text, row["text"], ignore), row) for row in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )
        old = self.rows("select cluster_id from documents where doc_id = %s", (doc_id,))
        cluster_id = None
        if scored and scored[0][0] >= DUPLICATE_AT:
            best = scored[0][1]
            cluster_id = best["cluster_id"] or best["doc_id"]
            self.run(
                "update documents set cluster_id = %s where doc_id = %s",
                (cluster_id, best["doc_id"]),
            )
        elif scored and scored[0][0] >= POSSIBLE_DUPLICATE_AT:
            details = {"candidate": scored[0][1]["doc_id"], "containment": round(scored[0][0], 2)}
            self.review(doc_id, part.key, "possible_duplicate", details)
        self.run("update documents set cluster_id = %s where doc_id = %s", (cluster_id, doc_id))
        for cluster in {cluster_id, old[0]["cluster_id"] if old else None} - {None}:
            self.elect_canonical(str(cluster))
        if cluster_id is None:
            self.run("update documents set is_canonical = true where doc_id = %s", (doc_id,))

    def same_type_texts(self, doc_type: str) -> list[dict[str, Any]]:
        # Fine at this scale. With many thousands of documents, store phrase hashes per
        # version and count them in SQL instead of reading every text.
        return self.rows(
            "select coalesce(d.cluster_id, d.doc_id) as group_key, v.parsed ->> 'text' as text"
            " from documents d"
            " join document_versions v on v.doc_id = d.doc_id and v.version = d.current_version"
            " where d.doc_type = %s and d.status <> 'deleted' order by d.doc_id limit %s",
            (doc_type, BOILERPLATE_SAMPLE),
        )

    def elect_canonical(self, cluster_id: str) -> None:
        members = self.rows(
            "select doc_id, origin, status, effective_date, verified, confidence from documents"
            " where cluster_id = %s and status <> 'deleted'",
            (cluster_id,),
        )
        if not members:
            return
        # Prefer the templated document, then a current one, then the newest, then the best read.
        winner = max(
            members,
            key=lambda m: (
                m["origin"] == "greenfield",
                m["status"] == "active",
                m["effective_date"] or date.min,
                m["verified"],
                m["confidence"],
            ),
        )
        self.run(
            "update documents set is_canonical = (doc_id = %s) where cluster_id = %s",
            (winner["doc_id"], cluster_id),
        )

    def tombstone(self, doc_id: str) -> None:
        self.run(
            "update documents set status = 'deleted', updated_at = now() where doc_id = %s",
            (doc_id,),
        )
        self.run("delete from chunks where doc_id = %s", (doc_id,))
        self.run("delete from sop_steps where doc_id = %s", (doc_id,))
        cluster = self.rows("select cluster_id from documents where doc_id = %s", (doc_id,))
        if cluster and cluster[0]["cluster_id"]:
            self.elect_canonical(cluster[0]["cluster_id"])

    def refresh_superseded(self) -> None:
        superseded = (
            "select l.to_doc from doc_links l join documents d on d.doc_id = l.from_doc"
            " where l.kind = 'supersedes' and d.status = 'active'"
        )
        self.run(
            "update documents set status = 'superseded'"
            f" where status = 'active' and doc_id in ({superseded})"
        )
        self.run(
            "update documents set status = 'active'"
            f" where status = 'superseded' and doc_id not in ({superseded})"
        )

    def link(self, from_doc: str, to_doc: str, kind: str) -> None:
        self.run(
            "insert into doc_links (from_doc, to_doc, kind) values (%s, %s, %s)"
            " on conflict do nothing",
            (from_doc, to_doc, kind),
        )

    def review(
        self, doc_id: str | None, source_key: str, reason: str, details: dict[str, Any]
    ) -> None:
        self.run(
            "insert into review_items (doc_id, source_key, reason, details)"
            " values (%s, %s, %s, %s)",
            (doc_id, source_key, reason, json.dumps(details)),
        )

    def rows(self, sql: str, params: Any) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def run(self, sql: str, params: Any = None) -> None:
        self.conn.execute(sql, params)


def as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
