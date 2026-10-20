from datetime import date
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from rootline.core.models import SopStep

DOCUMENT = """
    select d.doc_id, d.doc_type, d.title, d.status, d.origin, d.services,
        d.current_version as version, d.effective_date, d.needs_review, d.verified, v.record
    from documents d
    join document_versions v on v.doc_id = d.doc_id and v.version = d.current_version
    where d.doc_id = %s and d.status <> 'deleted'
"""
# Steps come from sop_steps, the rows checked word for word against the source at ingest.
STEPS = """
    select section, ordinal, instruction, command, action, check_name, expect
    from sop_steps where doc_id = %s and version = %s
    order by section = 'rollback', ordinal
"""


class Document(BaseModel):
    doc_id: str
    doc_type: str
    title: str
    status: str
    origin: str
    services: list[str]
    version: int
    effective_date: date | None
    needs_review: bool
    verified: bool
    record: dict[str, Any]
    steps: list[SopStep] = []
    rollback: list[SopStep] = []


def as_text(doc: Document) -> str:
    # The SOP's steps in plain text, for prompts: one line per step with its command.
    lines = [f"{doc.doc_id} v{doc.version} {doc.title}"]
    for name, steps in (("Steps", doc.steps), ("Rollback", doc.rollback)):
        lines += [f"{name}:"] if steps else []
        for step in steps:
            parts = [f"{step.ordinal}. {step.instruction}"]
            parts += [f"command: {step.command}"] if step.command else []
            parts += [f"expect: {step.expect}"] if step.expect else []
            lines.append(" | ".join(parts))
    return "\n".join(lines)


def read_document(conn: psycopg.Connection[Any], doc_id: str) -> Document | None:
    with conn.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(DOCUMENT, (doc_id,)).fetchone()
        if row is None:
            return None
        rows = cursor.execute(STEPS, (doc_id, row["version"])).fetchall()
    sections: dict[str, list[SopStep]] = {"steps": [], "rollback": []}
    for step in rows:
        sections[step.pop("section")].append(SopStep.model_validate(step))
    return Document.model_validate(row | sections)
