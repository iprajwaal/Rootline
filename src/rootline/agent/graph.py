import logging
import time
import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from rootline.agent.guard import MAX_BYTES, GuardReport, guard
from rootline.agent.state import AgentState, Step
from rootline.agent.understand import Understander, intent_asker
from rootline.catalog import load_catalog
from rootline.config import Settings
from rootline.llm.ledger import PostgresLedger, elapsed_ms, request_scope
from rootline.llm.structured import build_llm
from rootline.llm.tracing import tracing_callbacks

log = logging.getLogger(__name__)

Graph = CompiledStateGraph[AgentState, None, AgentState, AgentState]


def build_graph(understander: Understander, tracing: list[BaseCallbackHandler]) -> Graph:
    def guard_input(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        last = state["messages"][-1]
        guarded = guard(last.text)
        if guarded.report.injection:
            log.warning("injection flagged rules=%s", ",".join(guarded.report.injection))
        return {
            "request_id": uuid.uuid4().hex,
            "guard": guarded.report,
            # The cleaned text replaces the message, so a pasted secret never reaches the
            # thread history or a later prompt.
            "messages": [HumanMessage(content=guarded.text, id=last.id)],
            "trace": [Step(node="guard", detail=describe(guarded.report), ms=elapsed_ms(started))],
        }

    def understand(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        with request_scope(state["request_id"]):
            found = understander.understand(state["messages"][-1].text)
        detail = (
            f"{found.intent} · {found.lane or 'no list'} · {found.confidence:.2f} by {found.method}"
        )
        step = Step(
            node="understand",
            detail=detail,
            ms=elapsed_ms(started),
            llm_calls=int(found.method == "llm"),
        )
        return {"understanding": found, "trace": [step]}

    builder = StateGraph(AgentState)
    builder.add_node("guard", guard_input)
    builder.add_node("understand", understand)
    builder.add_node("reply", reply)
    builder.add_edge(START, "guard")
    builder.add_edge("guard", "understand")
    builder.add_edge("understand", "reply")
    builder.add_edge("reply", END)
    # With Langfuse keys set, every node run and the LLM calls inside it land in one trace.
    return builder.compile().with_config(callbacks=tracing)


def reply(state: AgentState) -> dict[str, Any]:
    found, report = state["understanding"], state["guard"]
    lines = [
        f"Understood as {found.intent} ({found.lane or 'no list'}),"
        f" confidence {found.confidence:.2f} by {found.method}."
    ]
    if found.alternatives:
        lines.append(f"Could also be: {', '.join(found.alternatives)}.")
    if report.redacted:
        lines.append(
            f"Removed {len(report.redacted)} secret(s) from your message before storing it."
        )
    if report.injection:
        lines.append(
            "Parts of your message read like instructions to the assistant."
            " They were treated as plain text."
        )
    return {"messages": [AIMessage(content="\n".join(lines))]}


def describe(report: GuardReport) -> str:
    parts = [f"cut to {MAX_BYTES // 1000} KB" if report.truncated else "size ok"]
    if report.redacted:
        parts.append(f"redacted {', '.join(report.redacted)}")
    if report.injection:
        parts.append(f"injection flagged: {', '.join(report.injection)}")
    return " · ".join(parts)


def default_graph(settings: Settings) -> Graph:
    llm = build_llm(settings, PostgresLedger(settings.database_url).record)
    understander = Understander(load_catalog(settings.catalog_path), intent_asker(llm))
    return build_graph(understander, tracing_callbacks(settings))


graph = default_graph(Settings())
