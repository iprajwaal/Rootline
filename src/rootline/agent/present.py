import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from rootline.agent.answer import Claim
from rootline.agent.state import Step
from rootline.agent.understand import Lane
from rootline.core.models import SopStep
from rootline.retrieval.documents import Document
from rootline.retrieval.passages import Passage
from rootline.retrieval.search import LANES, Hit, LaneResult, SearchResponse
from rootline.retrieval.suggest import NONE_FOUND, SuggestionLane, SuggestResponse

BADGE = {"rca": "[RCA]", "sop": "[SOP]", "ref": "[REFERENCE]"}
LIST_NAME = {"rca": "Past incidents", "sop": "Procedures"}
# Escaped so stored text shows exactly as written: DB_POOL_MAX must not turn into italics.
MARKDOWN = re.compile(r"([\\`*_\[\]<>#|~])")
BACKTICKS = re.compile(r"`+")
TIE = "The first two fit about equally well. Say which one you mean."
EXCERPTS = 2


@dataclass(frozen=True)
class Presenter:
    base_url: str
    stale_after_days: int
    today: Callable[[], date] = date.today

    def found(self, response: SearchResponse, lane: Lane | None) -> str:
        parts = [escape(response.message)] if response.message else []
        for name in LANES if lane in (None, "both") else (lane,):
            result: LaneResult = getattr(response, name)
            if result.hits:
                parts.append(f"**{LIST_NAME[name]} {escape(BADGE[name])}**")
                parts += [self.card(hit, " · ".join(hit.evidence)) for hit in result.hits]
                parts += [TIE] if result.ambiguous else []
            elif response.mode == "hybrid":
                parts.append(f"Nothing relevant in the {name.upper()}s.")
        if response.did_you_mean:
            parts.append("**Also close**")
            parts += [self.card(hit) for hit in response.did_you_mean]
        return "\n\n".join(parts)

    def suggestions(self, response: SuggestResponse) -> str:
        issue = response.issue
        read = [
            f"alert {', '.join(issue.alerts)}" if issue.alerts else "",
            f"error {issue.errors[0]}" if issue.errors else "",
            f"service {', '.join(issue.services)}" if issue.services else "",
        ]
        parts = [f"Read as: {escape(' · '.join(r for r in read if r))}"] if any(read) else []
        for name in LANES:
            result: SuggestionLane = getattr(response, name)
            parts.append(f"**{LIST_NAME[name]} {escape(BADGE[name])}**")
            if result.nothing_relevant:
                parts.append(NONE_FOUND[name])
                continue
            parts += [self.card(s, escape(s.why)) for s in result.suggestions]
            parts += [TIE] if result.ambiguous else []
        if issue.undocumented_alerts:
            parts.append(f"No document lists the alert {', '.join(issue.undocumented_alerts)}.")
        return "\n\n".join(parts)

    def procedure(self, doc: Document, hit: Hit) -> str:
        facts = [
            self.freshness(doc.effective_date),
            ", ".join(doc.services),
            *flags(hit),
            f"[open the full SOP]({self.link(doc.doc_id)})",
        ]
        parts = [
            f"**{escape(BADGE['sop'])} {doc.doc_id} · v{doc.version}** {escape(doc.title)}  \n"
            + " · ".join(fact for fact in facts if fact)
        ]
        if not doc.steps:
            parts.append("No steps were taken from this SOP. Open the full document instead.")
        else:
            parts.append("**Steps**\n" + checklist(doc.steps))
        if doc.rollback:
            parts.append("**Rollback**\n" + checklist(doc.rollback))
        related = [link for link in hit.linked if link.relation != "superseded_by"]
        if related:
            links = [
                f"[{escape(BADGE[link.doc_type])} {link.doc_id}]({self.link(link.doc_id)})"
                f" {escape(link.title)}"
                for link in related
            ]
            parts.append("**Related**\n" + "\n".join(f"- {link}" for link in links))
        return "\n\n".join(parts)

    def rca_answer(self, claims: list[Claim], passages: list[Passage]) -> str:
        sentences = [f"{escape(c.text)} {self.cite(c.sources, passages)}" for c in claims]
        used = sorted({n for claim in claims for n in claim.sources})
        sources = [f"{n}. {self.source(passages[n - 1])}" for n in used]
        return "\n\n".join(
            [
                f"**{escape(BADGE['rca'])} From past incidents**",
                " ".join(sentences),
                "**Sources**\n" + "\n".join(sources),
            ]
        )

    def unanswered(self, hits: list[Hit]) -> str:
        note = "The closest RCAs do not answer this question. They are linked here to check."
        return "\n\n".join([note, *(self.card(hit) for hit in hits)])

    def excerpts(self, note: str, passages: list[Passage]) -> str:
        parts = [note]
        for passage in passages[:EXCERPTS]:
            quoted = "\n".join(f"> {escape(line)}" for line in passage.text.splitlines())
            parts.append(f"{self.source(passage)}\n\n{quoted}")
        return "\n\n".join(parts)

    def cite(self, numbers: list[int], passages: list[Passage]) -> str:
        return "".join(f"[\\[{n}\\]]({self.link(passages[n - 1].doc_id)})" for n in numbers)

    def source(self, passage: Passage) -> str:
        badge = f"{escape(BADGE['rca'])} {passage.doc_id}"
        return (
            f"[{badge}]({self.link(passage.doc_id)}) · {escape(passage.section)}"
            f" · {escape(passage.title)}"
        )

    def choose(self, hits: list[Hit]) -> str:
        cards = [self.card(hit, " · ".join(hit.evidence)) for hit in hits]
        return "\n\n".join(
            ["Two procedures fit about equally well. Which one do you mean?", *cards]
        )

    def card(self, hit: Hit, why: str = "") -> str:
        facts = [
            f"v{hit.version}" if hit.version else "",
            self.freshness(hit.effective_date),
            ", ".join(hit.services),
            *flags(hit),
            f"[open]({self.link(hit.doc_id)})",
        ]
        lines = [
            f"**{escape(BADGE[hit.doc_type])} {hit.doc_id}** {escape(hit.title)}",
            " · ".join(fact for fact in facts if fact),
        ]
        return "  \n".join([*lines, why] if why else lines)

    def freshness(self, effective: date | None) -> str:
        if effective is None:
            return "no date on record"
        note = f"updated {effective.isoformat()}"
        if (self.today() - effective).days > self.stale_after_days:
            note += f", over {self.stale_after_days} days old, check it still holds"
        return note

    def link(self, doc_id: str) -> str:
        return f"{self.base_url}/docs/{doc_id}"


def flags(hit: Hit) -> list[str]:
    newer = [link.doc_id for link in hit.linked if link.relation == "superseded_by"]
    notes = []
    if hit.status == "superseded":
        notes.append(f"superseded by {', '.join(newer)}" if newer else "superseded")
    if hit.needs_review:
        notes.append("not reviewed yet")
    if not hit.verified:
        notes.append("not verified against its source")
    if hit.similar_copies:
        plural = "copy" if hit.similar_copies == 1 else "copies"
        notes.append(f"{hit.similar_copies} similar {plural} merged")
    return notes


def checklist(steps: list[SopStep]) -> str:
    items = []
    for step in steps:
        item = f"- [ ] **{step.ordinal}.** {escape(step.instruction)}"
        if step.command:
            item += f"  \n  Run: {code(step.command)}"
        if step.expect:
            item += f"  \n  Expect: {escape(step.expect)}"
        items.append(item)
    return "\n".join(items)


def footer(steps: list[Step]) -> str:
    chain = " → ".join(escape(step.node) for step in steps)
    calls = sum(step.llm_calls for step in steps)
    ms = sum(step.ms for step in steps)
    return f"\n\n---\n*steps: {chain} · LLM calls {calls} · {ms:.0f} ms*"


def escape(text: str) -> str:
    return MARKDOWN.sub(r"\\\1", text)


def code(text: str) -> str:
    # A code span needs a longer run of backticks than any inside the command.
    longest = max((len(run) for run in BACKTICKS.findall(text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text[:1] == "`" or text[-1:] == "`" else ""
    return f"{fence}{pad}{text}{pad}{fence}"
