import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from rootline.core.interfaces import Classifier
from rootline.core.models import Block, Classification, ParsedDoc
from rootline.ingest.document_builder import DocBuilder
from rootline.llm.structured import LLMUnavailable

Side = Literal["rca", "sop", ""]

# Rule scores between these bounds are too close to call and go to the LLM.
UNSURE_LOW, UNSURE_HIGH = 0.3, 0.7
REVIEW_BELOW = 0.7
# An empty or binary file has no real words; even a one-line note has several. Below this
# it is held for a person, with no model call.
MIN_WORDS = 5
REAL_WORD = re.compile(r"\b[a-z]{2,20}\b")
MIXED_MIN_SHARE = 0.25
PROMPT_CHARS = 3000
# Bump when the prompt wording changes. The ledger and traces group calls by it.
CLASSIFY_PROMPT_VERSION = "classify-v1"

STORY_SECTIONS = (
    "timeline",
    "root cause",
    "what happened",
    "why it happened",
    "impact",
    "problem statement",
    "summary",
    "follow-up",
    "follow up",
    "action items",
    "analysis",
    "whys",
    "fishbone",
    "lessons",
)
PROCEDURE_SECTIONS = (
    "steps",
    "procedure",
    "rollback",
    "to undo",
    "prerequisites",
    "before you start",
    "verification",
    "when to use",
    "troubleshooting",
    "contacts",
    "escalation",
)
STORY_WORDS = re.compile(
    r"(?i)\b(?:root cause|outage|incident|impact|postmortem|fired|was|were|happened|caused"
    r"|customers|affected|detected|resolved|recovered|failed|late)\b"
)
PROCEDURE_WORDS = re.compile(
    r"(?i)\b(?:run|check|confirm|restart|scale|rotate|roll back|verify|should see|expected"
    r"|kubectl|psql|curl|redis-cli|shopctl|kafka-consumer-groups|step)\b"
)
TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}\b")
STEP_MARKER = re.compile(r"^\d+[.)]\s")


class LlmVerdict(BaseModel):
    label: Literal["rca", "sop", "mixed"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


AskLlm = Callable[[str], LlmVerdict]


@dataclass(frozen=True)
class Scored:
    block: Block
    side: Side


@dataclass(frozen=True)
class RuleClassifier:
    def classify(self, doc: ParsedDoc) -> Classification:
        return rule_classification(score_blocks(doc))


@dataclass(frozen=True)
class LlmClassifier:
    ask_llm: AskLlm

    def classify(self, doc: ParsedDoc) -> Classification:
        verdict = self.ask_llm(llm_prompt(doc))
        return Classification(
            label=verdict.label,
            confidence=verdict.confidence,
            method="llm",
            evidence=[verdict.reason],
        )


RULES = RuleClassifier()


def classify(
    doc: ParsedDoc, ask_llm: AskLlm | None = None, first: Classifier = RULES
) -> Classification:
    if len(set(REAL_WORD.findall(doc.text.lower()))) < MIN_WORDS:
        return Classification(
            label="unknown", confidence=1.0, method="rules", evidence=["too little text"]
        )
    template = template_label(doc)
    if template:
        return Classification(
            label=template, confidence=1.0, method="template", evidence=["template structure"]
        )

    rules = RULES.classify(doc)
    # Mixed and empty documents are judged by their layout, which one score cannot show.
    guess = first.classify(doc) if rules.label in ("rca", "sop") else rules
    if guess.confidence >= UNSURE_HIGH or ask_llm is None:
        return guess
    try:
        verdict = LlmClassifier(ask_llm).classify(doc)
    except LLMUnavailable as error:
        return guess.model_copy(update={"evidence": [*guess.evidence, f"llm unavailable: {error}"]})

    # The model's own confidence is not calibrated. Its label is trusted only when the first
    # guess leans the same way; otherwise it is kept but held below the review line.
    agrees = verdict.label == guess.label
    confidence = verdict.confidence if agrees else min(verdict.confidence, REVIEW_BELOW - 0.01)
    note = (
        f"{guess.method} agrees"
        if agrees
        else f"{guess.method} said {guess.label}, needs a person to confirm"
    )
    return verdict.model_copy(
        update={"confidence": confidence, "evidence": [*verdict.evidence, note]}
    )


def needs_review(classification: Classification) -> bool:
    return classification.label == "unknown" or classification.confidence < REVIEW_BELOW


def template_label(doc: ParsedDoc) -> Literal["rca", "sop", "ref"] | None:
    declared = str(doc.fields.get("type", "")).lower()
    if declared in ("rca", "sop", "ref"):
        return declared  # type: ignore[return-value]
    if "Document title" in doc.fields and "Document number" in doc.fields:
        return "sop"
    if "RCA ID" in doc.fields:
        return "rca"
    return None


def score_blocks(doc: ParsedDoc) -> list[Scored]:
    scored = []
    section: Side = ""
    for block in doc.blocks:
        if block.kind == "heading":
            # The title is skipped on purpose: misleading titles are common in old docs.
            section = section_side(block.text) if block.level > 1 else section
            scored.append(Scored(block, ""))
        else:
            scored.append(Scored(block, section or text_side(block.text)))
    return scored


def section_side(heading: str) -> Side:
    text = heading.lower()
    if any(word in text for word in STORY_SECTIONS):
        return "rca"
    if any(word in text for word in PROCEDURE_SECTIONS):
        return "sop"
    return ""


def text_side(text: str) -> Side:
    story = len(STORY_WORDS.findall(text)) + 2 * len(TIMESTAMP.findall(text))
    procedure = len(PROCEDURE_WORDS.findall(text)) + (2 if STEP_MARKER.match(text) else 0)
    if story > procedure:
        return "rca"
    if procedure > story:
        return "sop"
    return ""


def rule_classification(scored: list[Scored]) -> Classification:
    share, total = procedure_share(scored)
    if not total:
        return Classification(label="unknown", confidence=0.0, method="rules", evidence=[])

    evidence = [f"procedure share {share:.2f} of {total} scored characters"]
    if min(share, 1 - share) >= MIXED_MIN_SHARE and len(regions(scored)) == 2:
        return Classification(label="mixed", confidence=0.8, method="rules", evidence=evidence)
    label: Literal["rca", "sop"] = "sop" if share >= 0.5 else "rca"
    return Classification(
        label=label, confidence=max(share, 1 - share), method="rules", evidence=evidence
    )


def procedure_share(scored: list[Scored]) -> tuple[float, int]:
    weights = {"rca": 0, "sop": 0}
    for item in scored:
        if item.side:
            weights[item.side] += len(item.block.text)
    total = weights["rca"] + weights["sop"]
    return (weights["sop"] / total if total else 0.5), total


def regions(scored: list[Scored]) -> list[Side]:
    # Contiguous runs of one side, ignoring runs too small to matter. A mixed document has a
    # clean incident part and a clean procedure part; a muddled one flips back and forth.
    total = sum(len(item.block.text) for item in scored if item.side)
    runs: list[tuple[Side, int]] = []
    for item in scored:
        if not item.side:
            continue
        if runs and runs[-1][0] == item.side:
            runs[-1] = (item.side, runs[-1][1] + len(item.block.text))
        else:
            runs.append((item.side, len(item.block.text)))
    sides: list[Side] = []
    for side, weight in runs:
        if weight >= 0.05 * total and (not sides or sides[-1] != side):
            sides.append(side)
    return sides


def llm_prompt(doc: ParsedDoc) -> str:
    return (
        "Classify this operations document.\n"
        "rca: it explains an incident after it happened (what broke, timeline, impact, cause).\n"
        "sop: it is a procedure to follow (steps, commands, checks, rollback).\n"
        "mixed: it clearly contains both an incident write-up and a procedure.\n"
        "Judge by the body, not the title, because titles are often misleading.\n\n"
        f"Title: {doc.title}\n\n{doc.text[:PROMPT_CHARS]}"
    )


def split_mixed(doc: ParsedDoc) -> tuple[ParsedDoc, ParsedDoc]:
    parts = {"rca": DocBuilder(), "sop": DocBuilder()}
    current: Side = ""
    for item in score_blocks(doc):
        block = item.block
        if block.kind == "heading" and block.level == 1:
            targets = list(parts.values())
        else:
            side = section_side(block.text) if block.kind == "heading" else item.side
            current = side or current
            targets = [parts[current]] if current else list(parts.values())
        for builder in targets:
            if block.kind == "table":
                builder.add_table(block.rows)
            else:
                builder.add(block.kind, block.text, block.level)
    return (
        parts["rca"].build(f"{doc.source_key}#rca", doc.source_format, {}, title=doc.title),
        parts["sop"].build(f"{doc.source_key}#sop", doc.source_format, {}, title=doc.title),
    )
