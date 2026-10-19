from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DocType = Literal["rca", "sop", "ref"]
BlockKind = Literal["heading", "paragraph", "list_item", "code", "table"]
FishboneCategory = Literal["process", "people", "tools", "environment", "data", "external"]


class Block(BaseModel):
    kind: BlockKind
    text: str
    level: int = 0
    start: int
    end: int


class ParsedDoc(BaseModel):
    source_key: str
    title: str
    fields: dict[str, Any] = {}
    blocks: list[Block]
    text: str


class DocMeta(BaseModel):
    doc_id: str
    doc_type: DocType
    title: str
    services: list[str]
    owner: str = ""
    revision: int = 1
    effective_date: str | None = None
    supersedes: str | None = None
    related: list[str] = []


class SopStep(BaseModel):
    ordinal: int
    instruction: str
    command: str | None = None
    action: str | None = None
    check_name: str | None = None
    expect: str | None = None
    responsibility: str | None = None
    source_start: int | None = None
    source_end: int | None = None


class Term(BaseModel):
    term: str
    meaning: str


class Role(BaseModel):
    role: str
    responsibility: str


class TroubleshootingEntry(BaseModel):
    symptom: str
    action: str


class Revision(BaseModel):
    revision: int
    date: str
    author: str
    change: str


class SopRecord(BaseModel):
    purpose: str = ""
    scope: str = ""
    when_to_use: str = ""
    alerts: list[str] = []
    definitions: list[Term] = []
    roles: list[Role] = []
    related_documents: list[str] = []
    prerequisites: list[str] = []
    overview: str = ""
    steps: list[SopStep]
    verification: list[str] = []
    rollback: list[SopStep] = []
    troubleshooting: list[TroubleshootingEntry] = []
    escalation: list[str] = []
    security_notes: list[str] = []
    compliance: list[str] = []
    review_cycle: str = ""
    records: str = ""
    revision_history: list[Revision] = []

    @field_validator("steps", "rollback")
    @classmethod
    def numbered_in_order(cls, steps: list[SopStep]) -> list[SopStep]:
        # A gap or a swap here means a step was lost or reordered during extraction.
        ordinals = [step.ordinal for step in steps]
        if ordinals != list(range(1, len(steps) + 1)):
            raise ValueError(f"steps must be numbered 1..{len(steps)} in order, got {ordinals}")
        return steps


class TimelineEvent(BaseModel):
    timestamp: str
    event: str
    action: str = ""
    responsibility: str = ""


class Why(BaseModel):
    question: str
    answer: str


class FishboneBranch(BaseModel):
    category: FishboneCategory
    issues: list[str]
    whys: list[str] = []


class ActionItem(BaseModel):
    action: str
    owner: str = ""
    due_date: str | None = None


class RcaRecord(BaseModel):
    solution: str = ""
    short_description: str = ""
    authors: list[str] = []
    stakeholders: list[str] = []
    status: str = ""
    environment: str = ""
    incident_date: str | None = None
    start_time: str | None = None
    duration: str = ""
    severity: str | None = None
    trigger: str = ""
    problem_statement: str = ""
    impact_description: str = ""
    impact_metrics: str = ""
    customer_feedback: str = ""
    immediate_resolution: str = ""
    timeline: list[TimelineEvent] = []
    five_whys: list[Why] = []
    fishbone: list[FishboneBranch] = []
    root_causes: list[str] = []
    action_items: list[ActionItem] = []


class Classification(BaseModel):
    label: Literal["rca", "sop", "mixed", "unknown"]
    confidence: float = Field(ge=0.0, le=1.0)
    method: Literal["template", "rules", "llm"]
    evidence: list[str] = []
