from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from rootline.models import DocMeta, RcaRecord, SopRecord, SopStep

COMPANY = "ShopLite Commerce"
EMPTY = "None."

SOP_SECTIONS = [
    "Purpose",
    "Scope",
    "When to use",
    "Definitions and abbreviations",
    "Roles and responsibilities",
    "Related documents",
    "Prerequisites",
    "Procedure overview",
    "Procedure",
    "Verification",
    "Rollback",
    "Troubleshooting",
    "Escalation",
    "Security considerations",
    "Compliance and standards",
    "Review and update",
    "Records",
]
REVISION_HISTORY = "Appendix A. Revision history"
STEP_COLUMNS = ["Step", "Action", "Command", "Expected result", "Responsibility", "Automation"]
PROCEDURE_RULE = (
    "Run the steps in order. Do not skip or reorder steps. "
    "Stop and escalate if a result does not match the expected result."
)
FISHBONE_LABELS = {
    "process": "Process",
    "people": "People",
    "tools": "Tools",
    "environment": "Environment",
    "data": "Data",
    "external": "External factors",
}


@dataclass(frozen=True)
class Heading:
    text: str
    level: int


@dataclass(frozen=True)
class Paragraph:
    text: str


@dataclass(frozen=True)
class Bullets:
    items: list[str]
    numbered: bool = False


@dataclass(frozen=True)
class Table:
    header: list[str]
    rows: list[list[str]]
    code_columns: frozenset[int] = frozenset()


Element = Heading | Paragraph | Bullets | Table


@dataclass(frozen=True)
class Layout:
    title: str
    author: str
    date: str
    header_lines: list[str]
    elements: list[Element]


@dataclass(frozen=True)
class Source:
    render_as: Literal["docx", "pdf", "md"]
    meta: DocMeta
    record: SopRecord | RcaRecord


def load_source(path: Path) -> Source:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    meta = DocMeta.model_validate(raw["meta"])
    record_type = SopRecord if meta.doc_type == "sop" else RcaRecord
    return Source(raw["render_as"], meta, record_type.model_validate(raw["record"]))


def layout_for(source: Source) -> Layout:
    if isinstance(source.record, SopRecord):
        return sop_layout(source.meta, source.record)
    return rca_layout(source.meta, source.record)


def sop_layout(meta: DocMeta, sop: SopRecord) -> Layout:
    sections: list[list[Element]] = [
        [Paragraph(sop.purpose)],
        [Paragraph(sop.scope)],
        [Paragraph(sop.when_to_use), Paragraph(f"Alerts: {joined(sop.alerts)}")],
        [Bullets([f"{item.term}: {item.meaning}" for item in sop.definitions])],
        [Table(["Role", "Responsibility"], [[r.role, r.responsibility] for r in sop.roles])],
        [Bullets(sop.related_documents)],
        [Bullets(sop.prerequisites)],
        [Paragraph(sop.overview)],
        [Paragraph(PROCEDURE_RULE), step_table(sop.steps)],
        [Bullets(sop.verification)],
        [step_table(sop.rollback)],
        [Table(["Symptom", "Action"], [[t.symptom, t.action] for t in sop.troubleshooting])],
        [Bullets(sop.escalation)],
        [Bullets(sop.security_notes)],
        [Bullets(sop.compliance)],
        [Paragraph(sop.review_cycle)],
        [Paragraph(sop.records)],
    ]
    revision = f"{meta.revision:02d}"
    elements: list[Element] = [
        Table(
            ["Document title", meta.title],
            [
                ["Document number", meta.doc_id],
                ["Revision", revision],
                ["Effective date", meta.effective_date or ""],
                ["Owner", meta.owner],
                ["Services", ", ".join(meta.services)],
                ["Alerts", joined(sop.alerts)],
                ["Supersedes", meta.supersedes or "None"],
            ],
        ),
        Heading("Standard operating procedure", 1),
        Heading(meta.title, 2),
        Heading("Table of contents", 2),
        Bullets(SOP_SECTIONS, numbered=True),
        Paragraph(REVISION_HISTORY),
    ]
    for number, (name, body) in enumerate(zip(SOP_SECTIONS, sections, strict=True), start=1):
        elements.append(Heading(f"{number}. {name}", 2))
        elements.extend(filled(element) for element in body)
    history = [[f"{r.revision:02d}", r.date, r.author, r.change] for r in sop.revision_history]
    elements.append(Heading(REVISION_HISTORY, 2))
    elements.append(filled(Table(["Revision", "Date", "Author", "Change"], history)))
    return Layout(
        title=meta.title,
        author=meta.owner,
        date=meta.effective_date or "",
        header_lines=[
            f"Document title: {meta.title}",
            f"Document number: {meta.doc_id}  Revision: {revision}",
        ],
        elements=elements,
    )


def rca_layout(meta: DocMeta, rca: RcaRecord) -> Layout:
    summary = [
        ["RCA ID", meta.doc_id],
        ["Date", meta.effective_date or ""],
        ["Short description", rca.short_description],
        ["Authors", ", ".join(rca.authors)],
        ["Stakeholders", ", ".join(rca.stakeholders)],
        ["Status", rca.status],
        ["Issue environment", rca.environment],
        ["Incident start time", rca.start_time or ""],
        ["Incident duration", rca.duration],
        ["Affected services or applications", ", ".join(meta.services)],
        ["Severity level", rca.severity or ""],
        ["Trigger", rca.trigger],
        ["Related SOPs", joined(meta.related)],
    ]
    timeline = [[e.timestamp, e.event, e.action, e.responsibility] for e in rca.timeline]
    actions = [[a.action, a.owner, a.due_date or ""] for a in rca.action_items]
    elements: list[Element] = [
        Paragraph(rca.solution),
        Heading(meta.title, 1),
        Paragraph("Detailed root cause analysis"),
        Paragraph(COMPANY),
        Heading("Summary", 2),
        Table(["Field", "Value"], summary),
        Heading("Problem statement", 2),
        Paragraph(rca.problem_statement),
        Heading("Impact", 2),
        Heading("Description of impact", 3),
        Paragraph(rca.impact_description),
        Heading("Key metrics affected", 3),
        Paragraph(rca.impact_metrics),
        Heading("Customer feedback", 3),
        Paragraph(rca.customer_feedback),
        Heading("Immediate resolution", 2),
        Paragraph(rca.immediate_resolution),
        Heading("Event timeline", 2),
        Table(["Timestamp", "Event description", "Action taken", "Responsibility"], timeline),
        Heading("Analysis", 2),
        Heading("5-whys analysis", 3),
    ]
    for number, why in enumerate(rca.five_whys, start=1):
        elements.append(Paragraph(f"{ordinal(number)} why"))
        elements.append(Bullets([f"Question: {why.question}", f"Answer: {why.answer}"]))
    elements.append(Heading("Fishbone analysis", 3))
    for branch in rca.fishbone:
        elements.append(Heading(FISHBONE_LABELS[branch.category], 4))
        elements.append(Bullets(branch.issues))
        if branch.whys:
            whys = [f"{ordinal(n)} why: {text}" for n, text in enumerate(branch.whys, start=1)]
            elements.append(Bullets(whys))
    elements += [
        Heading("Root causes", 2),
        Paragraph("Based on the analysis above, the root causes of the incident are:"),
        Bullets(rca.root_causes, numbered=True),
        Heading("Action items", 2),
        Paragraph("The following action items address the root causes above."),
        Table(["Action", "Owner", "Due date"], actions),
    ]
    return Layout(
        title=meta.title,
        author=", ".join(rca.authors) or meta.owner,
        date=meta.effective_date or "",
        header_lines=["Detailed root cause analysis", f"RCA ID: {meta.doc_id}"],
        elements=[filled(element) for element in elements],
    )


def step_table(steps: list[SopStep]) -> Table:
    rows = [
        [
            str(step.ordinal),
            step.instruction,
            step.command or "",
            step.expect or "",
            step.responsibility or "",
            automation(step),
        ]
        for step in steps
    ]
    return Table(STEP_COLUMNS, rows, code_columns=frozenset({2}))


def automation(step: SopStep) -> str:
    if step.action:
        return f"action:{step.action}"
    if step.check_name:
        return f"check:{step.check_name}"
    return ""


def filled(element: Element) -> Element:
    # Keep every template section present, so readers and parsers see "None." instead of a gap.
    match element:
        case Paragraph(text) if not text:
            return Paragraph(EMPTY)
        case Bullets(items) | Table(_, items) if not items:
            return Paragraph(EMPTY)
    return element


def joined(values: list[str]) -> str:
    return ", ".join(values) or "None"


def ordinal(number: int) -> str:
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(number if number < 20 else number % 10, "th")
    return f"{number}{suffix}"
