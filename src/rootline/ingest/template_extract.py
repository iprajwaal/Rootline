import re

from rootline.models import (
    ActionItem,
    Block,
    FishboneBranch,
    ParsedDoc,
    RcaRecord,
    Revision,
    Role,
    SopRecord,
    SopStep,
    Term,
    TimelineEvent,
    TroubleshootingEntry,
    Why,
)

SECTION_NUMBER = re.compile(r"^\d+\.\s*")
LIST_NUMBER = re.compile(r"^\d+\.\s+")
NTH_WHY = re.compile(r"^\d+(?:st|nd|rd|th) why:\s*")
EMPTY_VALUES = {"", "None.", "None", "TBD"}
FISHBONE_CATEGORIES = {
    "process": "process",
    "people": "people",
    "tools": "tools",
    "environment": "environment",
    "data": "data",
    "external factors": "external",
}

Sections = dict[str, list[Block]]


def template_sop(doc: ParsedDoc) -> SopRecord:
    s = sections(doc)
    when_to_use = [text for text in paragraphs(s, "when to use") if not text.startswith("Alerts:")]
    return SopRecord(
        purpose=joined(s, "purpose"),
        scope=joined(s, "scope"),
        when_to_use="\n\n".join(when_to_use),
        alerts=split_list(doc.fields.get("Alerts")),
        definitions=[
            Term(term=term, meaning=meaning)
            for term, _, meaning in (
                item.partition(": ") for item in items(s, "definitions and abbreviations")
            )
        ],
        roles=[Role(role=r[0], responsibility=r[1]) for r in rows(s, "roles and responsibilities")],
        related_documents=items(s, "related documents"),
        prerequisites=items(s, "prerequisites"),
        overview=joined(s, "procedure overview"),
        steps=[step(row) for row in rows(s, "procedure")],
        verification=items(s, "verification"),
        rollback=[step(row) for row in rows(s, "rollback")],
        troubleshooting=[
            TroubleshootingEntry(symptom=r[0], action=r[1]) for r in rows(s, "troubleshooting")
        ],
        escalation=items(s, "escalation"),
        security_notes=items(s, "security considerations"),
        compliance=items(s, "compliance and standards"),
        review_cycle=joined(s, "review and update"),
        records=joined(s, "records"),
        revision_history=[
            Revision(revision=int(r[0]), date=r[1], author=r[2], change=r[3])
            for r in rows(s, "appendix a. revision history")
        ],
    )


def template_rca(doc: ParsedDoc) -> RcaRecord:
    s, fields = sections(doc), doc.fields
    start_time = value(fields.get("Incident start time"))
    return RcaRecord(
        solution=doc.blocks[0].text if doc.blocks and doc.blocks[0].kind == "paragraph" else "",
        short_description=value(fields.get("Short description")),
        authors=split_list(fields.get("Authors")),
        stakeholders=split_list(fields.get("Stakeholders")),
        status=value(fields.get("Status")),
        environment=value(fields.get("Issue environment")),
        incident_date=start_time[:10] or None,
        start_time=start_time or None,
        duration=value(fields.get("Incident duration")),
        severity=value(fields.get("Severity level")) or None,
        trigger=value(fields.get("Trigger")),
        problem_statement=joined(s, "problem statement"),
        impact_description=joined(s, "description of impact"),
        impact_metrics=joined(s, "key metrics affected"),
        customer_feedback=joined(s, "customer feedback"),
        immediate_resolution=joined(s, "immediate resolution"),
        timeline=[
            TimelineEvent(timestamp=r[0], event=r[1], action=r[2], responsibility=r[3])
            for r in rows(s, "event timeline")
        ],
        five_whys=five_whys(s.get("5-whys analysis", [])),
        fishbone=fishbone(doc.blocks),
        root_causes=[
            LIST_NUMBER.sub("", text)
            for text in texts(s.get("root causes", []))
            if LIST_NUMBER.match(text)
        ],
        action_items=[
            ActionItem(action=r[0], owner=r[1], due_date=r[2] or None)
            for r in rows(s, "action items")
        ],
    )


def sections(doc: ParsedDoc) -> Sections:
    found: Sections = {}
    current: list[Block] | None = None
    for block in doc.blocks:
        if block.kind == "heading":
            current = found.setdefault(SECTION_NUMBER.sub("", block.text).lower(), [])
        elif current is not None:
            current.append(block)
    return found


def texts(blocks: list[Block], kinds: tuple[str, ...] = ("paragraph", "list_item")) -> list[str]:
    return [b.text for b in blocks if b.kind in kinds and b.text not in EMPTY_VALUES]


def paragraphs(s: Sections, name: str) -> list[str]:
    return texts(s.get(name, []), ("paragraph",))


def joined(s: Sections, name: str) -> str:
    return "\n\n".join(paragraphs(s, name))


def items(s: Sections, name: str) -> list[str]:
    return texts(s.get(name, []), ("list_item",))


def rows(s: Sections, name: str) -> list[list[str]]:
    table = next((b for b in s.get(name, []) if b.kind == "table"), None)
    return table.rows[1:] if table else []


def value(raw: object) -> str:
    text = str(raw or "").strip()
    return "" if text in EMPTY_VALUES else text


def split_list(raw: object) -> list[str]:
    return [part.strip() for part in value(raw).split(",") if part.strip()]


def step(row: list[str]) -> SopStep:
    kind, _, name = row[5].partition(":")
    return SopStep(
        ordinal=int(row[0]),
        instruction=row[1],
        command=row[2] or None,
        expect=row[3] or None,
        responsibility=row[4] or None,
        action=name if kind == "action" else None,
        check_name=name if kind == "check" else None,
    )


def five_whys(blocks: list[Block]) -> list[Why]:
    whys = []
    for block in blocks:
        if block.kind == "list_item" and block.text.startswith("Question: "):
            question = block.text.removeprefix("Question: ")
        elif block.kind == "list_item" and block.text.startswith("Answer: "):
            whys.append(Why(question=question, answer=block.text.removeprefix("Answer: ")))
    return whys


def fishbone(blocks: list[Block]) -> list[FishboneBranch]:
    branches: list[FishboneBranch] = []
    inside = False
    for block in blocks:
        heading = block.text.lower() if block.kind == "heading" else ""
        if heading:
            inside = heading == "fishbone analysis" or (inside and heading in FISHBONE_CATEGORIES)
            if inside and heading in FISHBONE_CATEGORIES:
                branches.append(
                    FishboneBranch.model_validate(
                        {"category": FISHBONE_CATEGORIES[heading], "issues": []}
                    )
                )
        elif inside and branches and block.kind == "list_item":
            if NTH_WHY.match(block.text):
                branches[-1].whys.append(NTH_WHY.sub("", block.text))
            else:
                branches[-1].issues.append(block.text)
    return branches
