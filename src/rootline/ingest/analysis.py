import hashlib
from dataclasses import dataclass
from typing import Literal

from rootline.catalog import Catalog
from rootline.ingest.chunking import Chunk, chunk_document
from rootline.ingest.classify import AskLlm, classify, split_mixed, template_label
from rootline.ingest.embedding import Embedder
from rootline.ingest.extract import AskRecord, Extraction, extract
from rootline.ingest.facts import extract_facts
from rootline.ingest.hashing import content_hash, raw_sha256
from rootline.ingest.parsing import parse_bytes
from rootline.ingest.redaction import redact_doc
from rootline.models import Classification, DocFacts, ParsedDoc

Label = Literal["rca", "sop", "ref"]
Origin = Literal["greenfield", "brownfield", "reference"]


@dataclass(frozen=True)
class Part:
    key: str
    doc: ParsedDoc
    label: Label
    origin: Origin
    doc_id: str
    classification: Classification
    extraction: Extraction
    facts: DocFacts
    chunks: list[Chunk]
    vectors: list[list[float]]


@dataclass(frozen=True)
class Analysis:
    raw_sha: str
    content_hash: str
    secret_kinds: list[str]
    classification: Classification
    parts: list[Part]


@dataclass(frozen=True)
class Analyzer:
    catalog: Catalog
    embedder: Embedder
    ask_label: AskLlm | None
    ask_record: AskRecord | None

    def analyse(self, source_key: str, data: bytes) -> Analysis:
        doc, secrets = redact_doc(parse_bytes(data, source_key))
        classification = classify(doc, self.ask_label)
        pieces: list[tuple[ParsedDoc, Label]] = []
        if classification.label == "mixed":
            rca, sop = split_mixed(doc)
            pieces = [(rca, "rca"), (sop, "sop")]
        elif classification.label != "unknown":
            pieces = [(doc, classification.label)]
        return Analysis(
            raw_sha=raw_sha256(data),
            content_hash=content_hash(doc),
            secret_kinds=secrets,
            classification=classification,
            parts=[self.part(piece, label, classification) for piece, label in pieces],
        )

    def part(self, doc: ParsedDoc, label: Label, classification: Classification) -> Part:
        from_template = template_label(doc) is not None
        extraction = extract(doc, label, from_template, self.ask_record)
        facts = extract_facts(doc, self.catalog)
        doc_id = facts.doc_id or minted_id(label, doc.source_key)
        chunks = chunk_document(doc, label, doc_id, facts, extraction.record)
        # Greenfield means a filled template with its own ID. A half-filled template with "TBD"
        # as its ID is treated like any other legacy document.
        origin: Origin = (
            "reference"
            if label == "ref"
            else "greenfield"
            if from_template and facts.doc_id
            else "brownfield"
        )
        if classification.label == "mixed":
            classification = classification.model_copy(
                update={"label": label, "evidence": [*classification.evidence, "split from mixed"]}
            )
        return Part(
            key=doc.source_key,
            doc=doc,
            label=label,
            origin=origin,
            doc_id=doc_id,
            classification=classification,
            extraction=extraction,
            facts=facts,
            chunks=chunks,
            vectors=self.embedder.documents([chunk.body for chunk in chunks]),
        )


def minted_id(label: str, source_key: str) -> str:
    # Legacy files have no ID. One derived from the source key stays the same on every re-run.
    return f"{label.upper()}-B-{hashlib.sha256(source_key.encode()).hexdigest()[:6].upper()}"
