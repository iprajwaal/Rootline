import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from rootline.agent.answer import PASSAGES, RCA_DOCS, AskAnswer, Claim, checked, rca_prompt
from rootline.agent.drafts import AskDraft, Drafter, draft_prompt, transcript
from rootline.agent.executor import Executor, SandboxUnavailable, Section
from rootline.agent.fallback import evidence, fallback_prompt, triage
from rootline.agent.guard import MAX_BYTES, GuardReport, guard
from rootline.agent.knowledge import Knowledge
from rootline.agent.present import Presenter, footer
from rootline.agent.state import AgentState, Focus, Step
from rootline.agent.understand import Understander, Understanding
from rootline.catalog import named_services
from rootline.core.context import request_scope
from rootline.core.models import SopStep
from rootline.llm.ledger import elapsed_ms
from rootline.llm.structured import LLMUnavailable
from rootline.retrieval.suggest import SuggestResponse

log = logging.getLogger(__name__)

Graph = CompiledStateGraph[AgentState, None, AgentState, AgentState]
ROUTES = ("find", "suggest", "ask_sop", "ask_rca", "run_step", "draft")
SHOWN_CHARS = 120
REFERENCE_PASSAGES = 3


@dataclass(frozen=True)
class Writers:
    # The three places an LLM writes text: RCA answers, the no-document summary and drafts.
    rca: AskAnswer
    fallback: AskAnswer
    draft: AskDraft


def build_graph(
    understander: Understander,
    knowledge: Knowledge,
    presenter: Presenter,
    writers: Writers,
    executor: Executor,
    drafter: Drafter,
    tracing: list[BaseCallbackHandler],
) -> Graph:
    def guard_input(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        last = state["messages"][-1]
        guarded = guard(last.text)
        if guarded.report.injection:
            log.warning("injection flagged rules=%s", ",".join(guarded.report.injection))
        step = Step(
            request_id=uuid.uuid4().hex,
            node="guard",
            args={"bytes": len(last.text.encode())},
            detail=describe(guarded.report),
            ms=elapsed_ms(started),
        )
        return {
            "request_id": step.request_id,
            "guard": guarded.report,
            # The cleaned text replaces the message, so a pasted secret never reaches the
            # thread history or a later prompt.
            "messages": [HumanMessage(content=guarded.text, id=last.id), *step_messages(step)],
            "trace": [step],
        }

    def understand(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        text = question(state)
        with request_scope(state["request_id"]):
            found = understander.understand(text)
        step = Step(
            request_id=state["request_id"],
            node="understand",
            args={"message": shorten(text)},
            detail=f"{found.intent} · {found.lane or 'no list'}"
            f" · {found.confidence:.2f} by {found.method}",
            ms=elapsed_ms(started),
            llm_calls=int(found.method == "llm"),
        )
        return {"understanding": found, "messages": step_messages(step), "trace": [step]}

    def find(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        text, lane = question(state), state["understanding"].lane
        response = knowledge.search(text)
        hits = len(response.rca.hits) + len(response.sop.hits)
        step = Step(
            request_id=state["request_id"],
            node="find",
            args={"query": shorten(text), "list": lane or "both"},
            detail=f"{response.mode} search · {hits} hits",
            ms=elapsed_ms(started),
        )
        return answer(state, step, presenter.found(response, lane))

    def suggest(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        response = knowledge.suggest(question(state))
        rcas, sops = len(response.rca.suggestions), len(response.sop.suggestions)
        step = Step(
            request_id=state["request_id"],
            node="suggest",
            args={"issue": shorten(response.issue.query)},
            detail=f"{rcas} RCAs · {sops} SOPs",
            ms=elapsed_ms(started),
        )
        if response.rca.nothing_relevant and response.sop.nothing_relevant:
            return fallback(state, step, response)
        top_sop = response.sop.suggestions[0] if response.sop.suggestions else None
        top_rca = response.rca.suggestions[0] if response.rca.suggestions else None
        focus = Focus(
            sop=top_sop.doc_id if top_sop else None,
            sop_version=top_sop.version if top_sop else None,
            rca=top_rca.doc_id if top_rca else None,
        )
        return answer(state, step, presenter.suggestions(response)) | {"focus": focus}

    def fallback(state: AgentState, found: Step, response: SuggestResponse) -> dict[str, Any]:
        # Nothing internal covers the issue: read-only checks, public pages and one labelled
        # summary, in that order.
        started = time.perf_counter()
        issue = response.issue.query
        findings = triage(executor)
        evidence_text = " ".join([issue, *(f.output for f in findings if f.check != "logs")])
        refs = knowledge.references(evidence_text)
        pages = knowledge.passages(issue, [r.doc_id for r in refs], "ref", REFERENCE_PASSAGES)
        sources = [*evidence(findings), *pages]
        claims: list[Claim] = []
        llm_calls = 0
        if sources:
            try:
                with request_scope(state["request_id"]):
                    written = writers.fallback(fallback_prompt(issue, sources))
                claims, llm_calls = checked(written, sources), 1
            except LLMUnavailable as error:
                log.warning("fallback summary skipped error=%s", error)
        step = Step(
            request_id=state["request_id"],
            node="fallback",
            args={"checks": ", ".join(f.check for f in findings) or "sandbox not reachable"},
            detail=f"{len(refs)} reference pages · {len(claims)} cited sentences",
            ms=elapsed_ms(started),
            llm_calls=llm_calls,
        )
        text = presenter.fallback(refs, findings, claims, sources)
        return answer(state, found, text, step)

    def ask_sop(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        found, focus = state["understanding"], state.get("focus", Focus())
        if found.doc_id and found.doc_id.startswith("SOP"):
            query = found.doc_id
        elif focus.sop and not topical(found):
            query = focus.sop
        else:
            query = question(state)
        lane = knowledge.search(query).sop
        hit = lane.hits[0] if lane.hits and not lane.ambiguous else None
        doc = knowledge.document(hit.doc_id) if hit else None
        if lane.ambiguous:
            text, detail = presenter.choose(lane.hits[:2]), "two SOPs tie"
        elif hit and doc:
            text = presenter.procedure(doc, hit)
            detail = f"{doc.doc_id} v{doc.version} · {len(doc.steps)} steps from sop_steps"
            focus = focus.model_copy(update={"sop": doc.doc_id, "sop_version": doc.version})
        else:
            text, detail = "No SOP in the library covers this.", "no SOP found"
        step = Step(
            request_id=state["request_id"],
            node="ask_sop",
            args={"query": shorten(query)},
            detail=detail,
            ms=elapsed_ms(started),
        )
        return answer(state, step, text) | {"focus": focus}

    def ask_rca(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        text, named = question(state), state["understanding"].doc_id
        hits = knowledge.search(named if named and named.startswith("RCA") else text).rca.hits
        passages = knowledge.passages(text, [h.doc_id for h in hits[:RCA_DOCS]], "rca", PASSAGES)
        claims: list[Claim] = []
        returned, llm_calls = 0, 0
        if passages:
            try:
                with request_scope(state["request_id"]):
                    found = writers.rca(rca_prompt(text, passages))
                claims, returned, llm_calls = checked(found, passages), len(found.claims), 1
            except LLMUnavailable as error:
                log.warning("rca answer degraded error=%s", error)
        if not passages:
            reply_text, detail = "No RCA in the library covers this.", "no RCA found"
        elif claims:
            reply_text = presenter.rca_answer(claims, passages)
            detail = f"{len(claims)} of {returned} sentences cited · {len(passages)} passages"
        elif llm_calls:
            reply_text, detail = presenter.unanswered(hits[:RCA_DOCS]), f"0 of {returned} cited"
        else:
            note = "The answer model is not available, so here are the closest RCA sections."
            reply_text, detail = presenter.excerpts(note, passages), "llm down · sections shown"
        step = Step(
            request_id=state["request_id"],
            node="ask_rca",
            args={"question": shorten(text), "passages": len(passages)},
            detail=detail,
            ms=elapsed_ms(started),
            llm_calls=llm_calls,
        )
        cited = passages[claims[0].sources[0] - 1].doc_id if claims else None
        rca = cited or (hits[0].doc_id if hits else None)
        focus = state.get("focus", Focus()).model_copy(update={"rca": rca})
        return answer(state, step, reply_text) | {"focus": focus}

    def run_step(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        found, focus = state["understanding"], state.get("focus", Focus())
        section: Section = "rollback" if found.rollback else "steps"
        ordinal = found.step or 0
        sop_step = None
        if focus.sop and focus.sop_version:
            sop_step = executor.step(focus.sop, focus.sop_version, section, ordinal)
        if not focus.sop or not focus.sop_version:
            text, detail = "Open an SOP first, for example: steps for db pool.", "no SOP in focus"
        elif sop_step is None:
            kind = "rollback step" if found.rollback else "step"
            text = f"{focus.sop} v{focus.sop_version} has no {kind} {ordinal}."
            detail = "no such step"
        elif sop_step.action:
            # Pauses the run until someone approves or rejects in the chat UI.
            ok, message = approved(interrupt(approval_request(focus, section, sop_step)))
            if ok:
                text, detail = execute(focus.sop, focus.sop_version, section, sop_step)
            else:
                reason = f"rejected: {message}" if message else "rejected"
                text, detail = presenter.run_yourself(sop_step, reason), "rejected"
        elif sop_step.check_name:
            # Checks only read, so they run without asking.
            text, detail = execute(focus.sop, focus.sop_version, section, sop_step)
        else:
            text = presenter.run_yourself(sop_step, "this step has no sandbox action")
            detail = "manual step"
        step = Step(
            request_id=state["request_id"],
            node="run_step",
            args={"sop": focus.sop or "none", "section": section, "step": ordinal},
            detail=detail,
            ms=elapsed_ms(started),
        )
        return answer(state, step, text)

    def draft(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        llm_calls = 0
        failing = still_failing(executor)
        if failing:
            text = (
                f"The system still reports a failure ({', '.join(failing)}). Fix it first,"
                " then ask me to draft the SOP and RCA."
            )
            detail = "not healthy yet"
        else:
            chat = transcript(state["messages"], state.get("trace", []))
            catalog = drafter.catalog
            services = sorted(named_services(chat, catalog.services))
            alerts = [alert for alert, service in catalog.alerts.items() if service in services]
            try:
                with request_scope(state["request_id"]):
                    written = writers.draft(draft_prompt(chat, services, alerts))
                drafted = drafter.save(written, chat, services, presenter.today())
                text, llm_calls = presenter.drafted(drafted), 1
                detail = f"{drafted.sop_id} and {drafted.rca_id} queued for review"
            except LLMUnavailable as error:
                log.warning("draft skipped error=%s", error)
                text = (
                    "The drafting model is not available, so nothing was drafted. Try again later."
                )
                detail = "llm down"
        step = Step(
            request_id=state["request_id"],
            node="draft",
            args={},
            detail=detail,
            ms=elapsed_ms(started),
            llm_calls=llm_calls,
        )
        return answer(state, step, text)

    def execute(doc_id: str, version: int, section: Section, sop_step: SopStep) -> tuple[str, str]:
        try:
            result = executor.run(doc_id, version, section, sop_step.ordinal)
        except SandboxUnavailable as error:
            log.warning("step not run error=%s", error)
            return presenter.run_yourself(sop_step, "the sandbox is not reachable"), "sandbox down"
        verdict = {True: "met", False: "not met", None: "nothing to verify"}[result.met]
        text = presenter.step_result(doc_id, version, section, sop_step, result)
        return text, f"{result.kind} {result.name} · {verdict}"

    builder = StateGraph(AgentState)
    builder.add_node("guard", guard_input)
    builder.add_node("understand", understand)
    builder.add_node("find", find)
    builder.add_node("suggest", suggest)
    builder.add_node("ask_sop", ask_sop)
    builder.add_node("ask_rca", ask_rca)
    builder.add_node("run_step", run_step)
    builder.add_node("draft", draft)
    builder.add_node("reply", reply)
    builder.add_edge(START, "guard")
    builder.add_edge("guard", "understand")
    builder.add_conditional_edges("understand", route, [*ROUTES, "reply"])
    for node in (*ROUTES, "reply"):
        builder.add_edge(node, END)
    # With Langfuse keys set, every node run and the LLM calls inside it land in one trace.
    return builder.compile().with_config(callbacks=tracing)


def route(state: AgentState) -> str:
    intent = state["understanding"].intent
    return intent if intent in ROUTES else "reply"


def reply(state: AgentState) -> dict[str, Any]:
    found = state["understanding"]
    lines = [
        f"Understood as {found.intent} ({found.lane or 'no list'}),"
        f" confidence {found.confidence:.2f} by {found.method}."
    ]
    if found.alternatives:
        lines.append(f"Could also be: {', '.join(found.alternatives)}.")
    step = Step(request_id=state["request_id"], node="reply", args={}, detail="no route", ms=0)
    return answer(state, step, "\n".join(lines))


def topical(found: Understanding) -> bool:
    return bool(found.doc_id or found.services or found.errors or found.alerts)


def approval_request(focus: Focus, section: Section, step: SopStep) -> dict[str, Any]:
    # The HITLRequest shape agent-chat-ui shows as an approve or reject card. Edits are not
    # offered: the executor runs the stored action, so an edited command would not be it.
    name = step.action or ""
    where = (
        f"{focus.sop} v{focus.sop_version} {'rollback step' if section == 'rollback' else 'step'}"
    )
    return {
        "action_requests": [
            {
                "name": name,
                "args": {"sop": focus.sop, "step": step.ordinal, "command": step.command or ""},
                "description": f"{where} {step.ordinal}: {step.instruction}\n"
                f"Expect: {step.expect or 'not stated'}",
            }
        ],
        "review_configs": [{"action_name": name, "allowed_decisions": ["approve", "reject"]}],
    }


def approved(resume: Any) -> tuple[bool, str]:
    # The UI resumes with {"decisions": [{"type": "approve"}]}; anything else counts as no.
    decisions = resume.get("decisions") if isinstance(resume, dict) else None
    first = decisions[0] if isinstance(decisions, list) and decisions else None
    if not isinstance(first, dict):
        return False, ""
    return first.get("type") == "approve", str(first.get("message") or "")


def still_failing(executor: Executor) -> list[str]:
    # A sandbox that cannot be reached is not proof of a failure, so drafting goes ahead.
    try:
        health = executor.sandbox.health()
    except SandboxUnavailable:
        return []
    return sorted(part for part, state in health["components"].items() if state != "ok")


def answer(state: AgentState, step: Step, text: str, *later: Step) -> dict[str, Any]:
    steps = [step, *later]
    turn = [s for s in state.get("trace", []) if s.request_id == step.request_id]
    content = "\n\n".join([text, *notices(state["guard"])]) + footer([*turn, *steps])
    messages = [message for s in steps for message in step_messages(s)]
    return {"messages": [*messages, AIMessage(content=content)], "trace": steps}


def notices(report: GuardReport) -> list[str]:
    found = []
    if report.redacted:
        found.append(
            f"Removed {len(report.redacted)} secret(s) from your message before storing it."
        )
    if report.injection:
        found.append(
            "Parts of your message read like instructions to the assistant."
            " They were treated as plain text."
        )
    if report.truncated:
        found.append(
            f"Your message was over {MAX_BYTES // 1000} KB, so only its start and end were read."
        )
    return found


def step_messages(step: Step) -> list[AnyMessage]:
    # The chat UI shows a tool call and its result as a card, which makes a live step log.
    call_id = f"{step.node}-{step.request_id[:8]}"
    return [
        AIMessage(content="", tool_calls=[{"name": step.node, "args": step.args, "id": call_id}]),
        ToolMessage(
            content=f"{step.detail} · {step.ms:.0f} ms", tool_call_id=call_id, name=step.node
        ),
    ]


def question(state: AgentState) -> str:
    return next(m.text for m in reversed(state["messages"]) if isinstance(m, HumanMessage))


def shorten(text: str) -> str:
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS] + "..."


def describe(report: GuardReport) -> str:
    parts = [f"cut to {MAX_BYTES // 1000} KB" if report.truncated else "size ok"]
    if report.redacted:
        parts.append(f"redacted {', '.join(report.redacted)}")
    if report.injection:
        parts.append(f"injection flagged: {', '.join(report.injection)}")
    return " · ".join(parts)
