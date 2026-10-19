from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DocType = Literal["rca", "sop", "ref"]
BlockKind = Literal["heading", "paragraph", "list_item", "code", "table"]


class Block(BaseModel):
    kind: BlockKind
    text: str
    level: int = 0
    start: int
    end: int


class ParsedDoc(BaseModel):
    source_key: str
    title: str
    front_matter: dict[str, Any] = {}
    blocks: list[Block]
    text: str


class SopStep(BaseModel):
    ordinal: int
    instruction: str
    command: str | None = None
    action: str | None = None
    check_name: str | None = None
    expect: str | None = None
    source_start: int | None = None
    source_end: int | None = None


class SopRecord(BaseModel):
    purpose: str = ""
    when_to_use: str = ""
    alerts: list[str] = []
    prerequisites: list[str] = []
    steps: list[SopStep]
    verification: list[str] = []
    rollback: list[SopStep] = []

    @field_validator("steps", "rollback")
    @classmethod
    def numbered_in_order(cls, steps: list[SopStep]) -> list[SopStep]:
        # A gap or a swap here means a step was lost or reordered during extraction.
        ordinals = [step.ordinal for step in steps]
        if ordinals != list(range(1, len(steps) + 1)):
            raise ValueError(f"steps must be numbered 1..{len(steps)} in order, got {ordinals}")
        return steps


class RcaRecord(BaseModel):
    summary: str = ""
    impact: str = ""
    severity: str | None = None
    incident_date: str | None = None
    timeline: list[str] = []
    root_cause: str = ""
    contributing_factors: list[str] = []
    resolution: str = ""
    action_items: list[str] = []


class Classification(BaseModel):
    label: Literal["rca", "sop", "mixed", "unknown"]
    confidence: float = Field(ge=0.0, le=1.0)
    method: Literal["front_matter", "rules", "llm"]
    evidence: list[str] = []
