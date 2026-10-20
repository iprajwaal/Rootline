import re
from dataclasses import dataclass

from rootline.core.models import Block, DocFacts, ParsedDoc, RcaRecord, SopRecord

# About 400 tokens: far under the embedding model's 2048, and small enough that a cited
# section points at the right few lines.
MAX_CHARS = 1600
CARD_SUMMARY_CHARS = 500
SECTION_NUMBER = re.compile(r"^\d+\.\s*")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
KIND_NAMES = {
    "sop": "standard operating procedure",
    "rca": "root cause analysis",
    "ref": "reference",
}
HEADER_ID = re.compile(r"^\[(\w+) · [^·\]]+ · ")


@dataclass(frozen=True)
class Chunk:
    section: str
    ordinal: int
    body: str


def chunk_document(
    doc: ParsedDoc, label: str, doc_id: str, facts: DocFacts, record: SopRecord | RcaRecord | None
) -> list[Chunk]:
    header = f"[{label.upper()} · {doc_id} · {', '.join(facts.services[:3]) or 'no service'}]"
    chunks = [Chunk("card", 0, card_text(doc, label, facts, record))]
    for section, blocks in group_sections(doc.blocks):
        for piece in pack(blocks):
            chunks.append(Chunk(section, len(chunks), f"{header} {section}\n{piece}"))
    return chunks


def stamp(body: str, doc_id: str, version: int) -> str:
    # The version is only known when the row is written, after the chunk was embedded.
    # A few characters of header do not move the vector, so it is not embedded again.
    return HEADER_ID.sub(lambda m: f"[{m.group(1)} · {doc_id} v{version} · ", body, count=1)


def card_text(
    doc: ParsedDoc, label: str, facts: DocFacts, record: SopRecord | RcaRecord | None
) -> str:
    if isinstance(record, SopRecord):
        summary = record.purpose or record.when_to_use
    elif isinstance(record, RcaRecord):
        summary = record.short_description or record.problem_statement
    else:
        summary = next((b.text for b in doc.blocks if b.kind == "paragraph"), "")
    lines = [
        f"{doc.title}",
        f"Type: {KIND_NAMES.get(label, label)}",
        f"Services: {', '.join(facts.services) or 'unknown'}",
    ]
    if facts.alerts:
        lines.append(f"Alerts: {', '.join(facts.alerts)}")
    if facts.error_signatures:
        lines.append(f"Errors: {'; '.join(facts.error_signatures[:5])}")
    lines.append(summary[:CARD_SUMMARY_CHARS])
    return "\n".join(lines)


def group_sections(blocks: list[Block]) -> list[tuple[str, list[Block]]]:
    sections: list[tuple[str, list[Block]]] = []
    for block in blocks:
        if block.kind == "heading" and block.level == 1:
            continue
        if block.kind == "heading" and block.level == 2:
            sections.append((SECTION_NUMBER.sub("", block.text), []))
        elif sections:
            sections[-1][1].append(block)
        else:
            sections.append(("Overview", [block]))
    return [(name, blocks) for name, blocks in sections if blocks]


def pack(blocks: list[Block]) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    for text in (part for block in blocks for part in block_parts(block)):
        if current and len("\n".join([*current, text])) > MAX_CHARS:
            pieces.append("\n".join(current))
            current = []
        current.append(text)
    if current:
        pieces.append("\n".join(current))
    return pieces


def block_parts(block: Block) -> list[str]:
    if block.kind == "heading":
        return [block.text]
    if block.kind == "list_item":
        return [f"- {block.text}"]
    if block.kind == "table" and len(block.text) > MAX_CHARS:
        # Split between rows and repeat the header, so no step is ever cut in half.
        header, *rows = block.text.split("\n")
        return [f"{header}\n{row}" for row in rows]
    if len(block.text) > MAX_CHARS:
        return SENTENCE_END.split(block.text)
    return [block.text]
