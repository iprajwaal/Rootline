import logging
import time
import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from rootline.agent.answer import PASSAGES, RCA_DOCS, AskAnswer, Claim, checked, rca_prompt
from rootline.agent.guard import MAX_BYTES, GuardReport, guard
from rootline.agent.knowledge import Knowledge
from rootline.agent.present import Presenter, footer
from rootline.agent.state import AgentState, Step
from rootline.agent.understand import Understander
from rootline.llm.ledger import elapsed_ms, request_scope
from rootline.llm.structured import LLMUnavailable

log = logging.getLogger(__name__)

Graph = CompiledStateGraph[AgentState, None, AgentState, AgentState]
ROUTES = ("find", "suggest", "ask_sop", "ask_rca")
SHOWN_CHARS = 120


def build_graph(
    understander: Understander,
    knowledge: Knowledge,
    presenter: Presenter,
    ask_answer: AskAnswer,
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
        return answer(state, step, presenter.suggestions(response))

    def ask_sop(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        named = state["understanding"].doc_id
        query = named if named and named.startswith("SOP") else question(state)
        lane = knowledge.search(query).sop
        hit = lane.hits[0] if lane.hits and not lane.ambiguous else None
        doc = knowledge.document(hit.doc_id) if hit else None
        if lane.ambiguous:
            text, detail = presenter.choose(lane.hits[:2]), "two SOPs tie"
        elif hit and doc:
            text = presenter.procedure(doc, hit)
            detail = f"{doc.doc_id} v{doc.version} · {len(doc.steps)} steps from sop_steps"
        else:
            text, detail = "No SOP in the library covers this.", "no SOP found"
        step = Step(
            request_id=state["request_id"],
            node="ask_sop",
            args={"query": shorten(query)},
            detail=detail,
            ms=elapsed_ms(started),
        )
        return answer(state, step, text)

    def ask_rca(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        text, named = question(state), state["understanding"].doc_id
        hits = knowledge.search(named if named and named.startswith("RCA") else text).rca.hits
        passages = knowledge.rca_passages(text, [h.doc_id for h in hits[:RCA_DOCS]], PASSAGES)
        claims: list[Claim] = []
        returned, llm_calls = 0, 0
        if passages:
            try:
                with request_scope(state["request_id"]):
                    found = ask_answer(rca_prompt(text, passages))
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
        return answer(state, step, reply_text)

    builder = StateGraph(AgentState)
    builder.add_node("guard", guard_input)
    builder.add_node("understand", understand)
    builder.add_node("find", find)
    builder.add_node("suggest", suggest)
    builder.add_node("ask_sop", ask_sop)
    builder.add_node("ask_rca", ask_rca)
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


def answer(state: AgentState, step: Step, text: str) -> dict[str, Any]:
    turn = [s for s in state.get("trace", []) if s.request_id == step.request_id]
    content = "\n\n".join([text, *notices(state["guard"])]) + footer([*turn, step])
    return {"messages": [*step_messages(step), AIMessage(content=content)], "trace": [step]}


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
