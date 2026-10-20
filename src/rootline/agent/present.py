import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from rootline.agent.answer import Claim
from rootline.agent.drafts import Drafted
from rootline.agent.executor import StepResult
from rootline.agent.fallback import Finding
from rootline.agent.state import PlanStep, Step
from rootline.agent.troubleshoot import ToolRun, trial_note
from rootline.agent.understand import Lane
from rootline.core.models import SopStep
from rootline.retrieval.documents import Document, anchor
from rootline.retrieval.passages import Passage
from rootline.retrieval.references import Reference
from rootline.retrieval.search import LANES, Hit, LaneResult, SearchResponse
from rootline.retrieval.suggest import NONE_FOUND, SuggestionLane, SuggestResponse

BADGE = {"rca": "[RCA]", "sop": "[SOP]", "ref": "[REFERENCE · external]"}
LIST_NAME = {"rca": "Past incidents", "sop": "Procedures"}
# Escaped so stored text shows exactly as written: DB_POOL_MAX must not turn into italics.
MARKDOWN = re.compile(r"([\\`*_\[\]<>#|~])")
BACKTICKS = re.compile(r"`+")
TIE = "The first two fit about equally well. Say which one you mean."
KEYWORDS_ONLY = (
    "Search by meaning is not available right now, so these results match keywords only."
)
EXCERPTS = 2
FOLLOW_UP = (
    "Run the steps in order and tell me what you see. Ask about any step, or say"
    ' "done" when you have finished and I will check the system.'
)
OUTPUT_CHARS = 160


@dataclass(frozen=True)
class Presenter:
    base_url: str
    stale_after_days: int
    today: Callable[[], date] = date.today

    def found(self, response: SearchResponse, lane: Lane | None) -> str:
        parts = [escape(response.message)] if response.message else []
        if response.mode == "lexical":
            parts.append(KEYWORDS_ONLY)
        for name in LANES if lane in (None, "both") else (lane,):
            result: LaneResult = getattr(response, name)
            if result.hits:
                parts.append(f"**{LIST_NAME[name]} {escape(BADGE[name])}**")
                parts += [self.card(hit, " · ".join(hit.evidence)) for hit in result.hits]
                parts += [TIE] if result.ambiguous else []
            elif response.mode in ("hybrid", "lexical"):
                parts.append(f"Nothing relevant in the {name.upper()}s.")
        if response.did_you_mean:
            parts.append("**Also close**")
            parts += [self.card(hit) for hit in response.did_you_mean]
        return "\n\n".join(parts)

    def suggestions(self, response: SuggestResponse, sop: Document | None) -> str:
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
        if sop and sop.steps:
            parts.append(
                f"**Steps from** {self.cited(f'{sop.doc_id} v{sop.version}')}\n"
                + checklist(sop.steps)
            )
            if sop.rollback:
                parts.append("**Rollback**\n" + checklist(sop.rollback))
            parts.append(FOLLOW_UP)
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
        return "".join(f"[\\[{n}\\]]({self.section(passages[n - 1])})" for n in numbers)

    def source(self, passage: Passage) -> str:
        badge = f"{escape(BADGE['rca'])} {passage.doc_id}"
        return (
            f"[{badge}]({self.section(passage)}) · {escape(passage.section)}"
            f" · {escape(passage.title)}"
        )

    def section(self, passage: Passage) -> str:
        # Opens the document page at the cited section.
        return f"{self.link(passage.doc_id)}#{anchor(passage.section)}"

    def tried(self, title: str, step: PlanStep, result: StepResult | None) -> str:
        parts = [f"**{escape(title)}** {escape(step.instruction)}"]
        if step.source:
            parts.append(f"From {self.cited(step.source)}")
        if step.command:
            parts.append(f"Run: {code(step.command)}")
        if result and result.kind == "check":
            parts.append(f"I ran this read-only check for you:\n\n{block(result.output)}")
        elif result:
            parts.append(f"Not run: {escape(trial_note(result))}, so it would not help now.")
        if step.expect:
            parts.append(f"You should see: {escape(step.expect)}")
        return "\n\n".join(parts)

    def ran(self, title: str, step: PlanStep, trial: StepResult, result: StepResult) -> str:
        parts = [f"**{escape(title)}** {escape(step.instruction)}"]
        if step.source:
            parts.append(f"From {self.cited(step.source)}")
        parts += [
            f"Ran after your approval: {code(step.command)}",
            f"Before running: {escape(trial_note(trial))}.",
            block(result.output or "(no output)"),
            f"**{escape(result.note.capitalize())}.**",
        ]
        if step.expect:
            parts.append(f"You should see: {escape(step.expect)}")
        return "\n\n".join(parts)

    def not_run(self, title: str, step: PlanStep, reason: str) -> str:
        parts = [
            f"**{escape(title)}** {escape(step.instruction)}",
            f"Not run, {escape(reason)}. Run it yourself:",
            block(step.command or step.instruction),
        ]
        if step.expect:
            parts.append(f"You should see: {escape(step.expect)}")
        return "\n\n".join(parts)

    def run_yourself(self, step: SopStep, reason: str) -> str:
        parts = [
            f"Not run, {escape(reason)}. Run it yourself:",
            block(step.command or step.instruction),
        ]
        if step.expect:
            parts.append(f"Expect: {escape(step.expect)}")
        return "\n\n".join(parts)

    def investigation(
        self,
        done: list[ToolRun],
        claims: list[Claim],
        sources: list[Passage],
        given: list[PlanStep],
        planned: bool,
        failing: list[str] | None,
        found: list[str],
    ) -> str:
        parts = []
        if claims:
            sentences = [f"{escape(c.text)} {self.cite_any(c.sources, sources)}" for c in claims]
            parts.append("**\\[AI · checked against what I looked at\\]** " + " ".join(sentences))
        else:
            parts.append("I could not work out more from what I looked at.")
        if given:
            parts.append("**Steps to follow**\n" + "\n".join(self.planned(s) for s in given))
            parts.append(FOLLOW_UP)
        elif planned:
            parts.append("The steps I gave earlier still stand.")
        if failing is None:
            parts.append("The sandbox is not reachable, so its state now is unknown.")
        elif failing:
            parts.append(f"Still failing: {escape(', '.join(failing))}.")
        else:
            parts.append("Health is ok now.")
        searched = any(run.tool == "search_docs" for run in done)
        if found:
            cards = "\n".join(f"- {self.cited(doc_id)}" for doc_id in found)
            parts.append(f"**Documents I checked**\n{cards}")
        elif searched:
            parts.append("No internal SOP or RCA covers this.")
        looked = [f"{n}. {looked_at(run)}" for n, run in enumerate(done, start=1)]
        looked += [f"{n}. {escape(s.title)}" for n, s in enumerate(sources, start=1)][len(done) :]
        parts.append("**What I looked at** (nothing was changed)\n" + "\n".join(looked))
        return "\n\n".join(parts)

    def fallback(
        self, references: list[Reference], findings: list[Finding], uncovered: bool
    ) -> str:
        parts = ["No internal SOP or RCA covers this."] if uncovered else []
        parts.append(
            "The assistant model is not available, so I could not investigate or test a fix."
            " Here is what the read-only checks and public pages show."
        )
        if references:
            pages = [
                f"- [{escape(ref.doc_id)}]({self.link(ref.doc_id)}) {escape(ref.title)}"
                f" · {escape(ref.publisher)} · {escape(ref.licence)} · [source]({ref.source_url})"
                for ref in references
            ]
            parts.append(
                f"**{escape(BADGE['ref'])}** Public documentation, not company procedure.\n"
                + "\n".join(pages)
            )
        if findings:
            lines = [
                f"- {escape(f.check)}: {escape(f.output)}" for f in findings if f.check != "logs"
            ]
            logs = [f.output for f in findings if f.check == "logs"]
            parts.append("**Read-only checks**\n" + "\n".join(lines))
            parts += [f"Recent logs\n\n{block(logs[0])}"] if logs else []
        return "\n\n".join(parts)

    def cite_any(self, numbers: list[int], sources: list[Passage]) -> str:
        # Checks have no page of their own, so only document sources get a link.
        cites = []
        for n in numbers:
            doc_id = sources[n - 1].doc_id
            cites.append(f"\\[{n}\\]" if doc_id == "CHECK" else f"[\\[{n}\\]]({self.link(doc_id)})")
        return "".join(cites)

    def drafted(self, drafted: Drafted) -> str:
        sop_note = f"update to v{drafted.sop_version}" if drafted.sop_version > 1 else "new"
        built = [f"built on {self.cited(drafted.based_on)}"] if drafted.based_on else []
        built += [f"related to {self.cited(doc_id)}" for doc_id in drafted.related]
        parts = [
            "Health is ok now, so I wrote up the incident from this chat. Nothing is searchable"
            " until a person approves it."
            + (" It replaces the drafts written earlier in this chat." if drafted.replaced else ""),
            f"- {escape(BADGE['sop'])} {drafted.sop_id} ({sop_note}) {escape(drafted.sop_title)}\n"
            f"- {escape(BADGE['rca'])} {drafted.rca_id} (new) {escape(drafted.rca_title)}",
            f"Review and approve them at {self.base_url}/review",
        ]
        if built:
            parts.insert(2, "The write-up is " + ", ".join(built) + ".")
        if drafted.dropped:
            commands = ", ".join(code(command) for command in drafted.dropped)
            parts.append(f"Left out commands that never appeared in this chat: {commands}")
        return "\n\n".join(parts)

    def planned(self, step: PlanStep) -> str:
        lines = [f"{step.ordinal}. {escape(step.instruction)}"]
        lines += [f"   Run: {code(step.command)}"] if step.command else []
        lines += [f"   You should see: {escape(step.expect)}"] if step.expect else []
        if step.changes:
            lines.append(f"   Changes the system. {escape(step.tested.capitalize())}.")
        if step.source:
            lines.append(f"   From {self.cited(step.source)}")
        return "  \n".join(lines)

    def cited(self, doc: str) -> str:
        # "SOP-DB-001 v2" or "RCA-2025-014": a badge and a link to the document's page.
        doc_id = doc.split()[0]
        kind = doc_id.split("-")[0].lower()
        badge = escape(BADGE.get(kind, "[DOC]"))
        return f"[{badge} {escape(doc)}]({self.link(doc_id)})"

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


def looked_at(run: ToolRun) -> str:
    output = " ".join(run.output.split())
    output = output if len(output) <= OUTPUT_CHARS else output[:OUTPUT_CHARS] + "..."
    parts = [escape(run.tool) + (f" {code(run.target)}" if run.target else "")]
    parts += [code(run.command)] if run.command else []
    return " · ".join(parts) + f" → {escape(output)}"


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


def block(text: str) -> str:
    longest = max((len(run) for run in BACKTICKS.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def code(text: str) -> str:
    # A code span needs a longer run of backticks than any inside the command.
    longest = max((len(run) for run in BACKTICKS.findall(text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text[:1] == "`" or text[-1:] == "`" else ""
    return f"{fence}{pad}{text}{pad}{fence}"
