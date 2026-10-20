import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from rootline.agent.answer import PASSAGES, RCA_DOCS, AskAnswer, Claim, checked, rca_prompt
from rootline.agent.drafts import AskDraft, Drafter, WriteUp, draft_prompt, transcript
from rootline.agent.executor import Executor, SandboxUnavailable, Section, StepResult
from rootline.agent.fallback import triage
from rootline.agent.guard import MAX_BYTES, GuardReport, guard
from rootline.agent.knowledge import Knowledge
from rootline.agent.present import FOLLOW_UP, Presenter, footer
from rootline.agent.state import AgentState, Focus, PlanStep, Step
from rootline.agent.toolbox import Toolbox, target
from rootline.agent.troubleshoot import (
    MAX_TOOL_CALLS,
    AskConclusion,
    AskTools,
    ToolRun,
    conclusion_prompt,
    documents,
    next_calls,
    pending,
    runs,
    shown,
    sources,
    system_prompt,
    task_prompt,
    trial_note,
    vetted,
)
from rootline.agent.understand import Understander, Understanding
from rootline.catalog import named_services
from rootline.core.context import request_scope
from rootline.core.models import SopStep
from rootline.llm.ledger import elapsed_ms
from rootline.llm.structured import LLMUnavailable
from rootline.ops import RequestLog, Turn

log = logging.getLogger(__name__)

Graph = CompiledStateGraph[AgentState, None, AgentState, AgentState]
ROUTES = (
    "find",
    "suggest",
    "ask_sop",
    "ask_rca",
    "run_step",
    "draft",
    "troubleshoot",
    "recheck",
)
SHOWN_CHARS = 120
NOTE_CHARS = 300


class Node(Protocol):
    def __call__(self, state: AgentState) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Writers:
    # Where an LLM writes: RCA answers, the agent's turns and conclusion, and drafts.
    rca: AskAnswer
    agent: AskTools
    conclusion: AskConclusion
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
    # Rootline never changes the system. It runs read-only checks, tries fixes on a copy and
    # tells the engineer what to run.
    toolbox = Toolbox(executor, knowledge)
    catalog = knowledge.catalog
    requests = RequestLog(knowledge.database_url)

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
        return answer(state, presenter.found(response, lane), step)

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
        issue = response.issue.query
        if response.rca.nothing_relevant and response.sop.nothing_relevant:
            # Nothing internal covers it, so the agent investigates next.
            return {"messages": step_messages(step), "trace": [step], "focus": Focus(issue=issue)}
        top_sop = response.sop.suggestions[0] if response.sop.suggestions else None
        top_rca = response.rca.suggestions[0] if response.rca.suggestions else None
        # A cause seen before gets its documents straight away, with the SOP's steps.
        doc = knowledge.document(top_sop.doc_id) if top_sop and not response.sop.ambiguous else None
        focus = Focus(
            sop=top_sop.doc_id if top_sop else None,
            sop_version=top_sop.version if top_sop else None,
            rca=top_rca.doc_id if top_rca else None,
            issue=issue,
        )
        return answer(state, presenter.suggestions(response, doc), step) | {"focus": focus}

    def troubleshoot(state: AgentState) -> dict[str, Any]:
        chat = transcript(state["messages"], state.get("trace", []))
        task = task_prompt(chat, state.get("focus", Focus()))
        return think_on(state, [SystemMessage(content=system_prompt(catalog)), HumanMessage(task)])

    def think(state: AgentState) -> dict[str, Any]:
        return think_on(state, state["work"])

    def think_on(state: AgentState, work: list[AnyMessage]) -> dict[str, Any]:
        started = time.perf_counter()
        used = len(runs(work))
        if used >= MAX_TOOL_CALLS:
            note = f"stopped: all {MAX_TOOL_CALLS} tool calls used"
            step = agent_step(state, "think", {"tool_calls": used}, note, started)
            return {"work": work, "messages": step_messages(step), "trace": [step]}
        try:
            with request_scope(state["request_id"]):
                decided = writers.agent(work)
        except LLMUnavailable as error:
            log.warning("agent turn failed error=%s", error)
            step = agent_step(state, "think", {"tool_calls": used}, "model not available", started)
            if not used:
                return fallback(state, step)
            return {"work": work, "messages": step_messages(step), "trace": [step]}
        note = shorten(decided.text, NOTE_CHARS) or "no notes"
        step = agent_step(
            state, "think", {"next": next_calls(decided) or "answer"}, note, started, 1
        )
        return {"work": [*work, decided], "messages": step_messages(step), "trace": [step]}

    def tools(state: AgentState) -> dict[str, Any]:
        # Every tool reads or tries on a copy, so all of them run without asking.
        work, results, steps = state["work"], [], []
        used = len(runs(work))
        for call in pending(work):
            started = time.perf_counter()
            if used >= MAX_TOOL_CALLS:
                results.append(tool_result(call["id"], call["name"], "not run: no tool calls left"))
                continue
            run = ToolRun(
                tool=call["name"],
                target=target(call["args"]),
                command=toolbox.command(call["name"], call["args"]),
                output=toolbox.run(call["name"], call["args"]),
            )
            used += 1
            results.append(tool_result(call["id"], call["name"], run.output, run))
            steps.append(agent_step(state, run.tool, {"target": run.target}, shown(run), started))
        return {
            "work": [*work, *results],
            "messages": [message for step in steps for message in step_messages(step)],
            "trace": steps,
        }

    def conclude(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        done, focus, asked = runs(state["work"]), state.get("focus", Focus()), question(state)
        cited = sources(done, asked, focus.plan)
        claims: list[Claim] = []
        given: list[PlanStep] = []
        llm_calls = 0
        try:
            with request_scope(state["request_id"]):
                written = writers.conclusion(conclusion_prompt(asked, cited, catalog))
            claims, llm_calls = checked(written, cited), 1
            given = vetted(written.steps, done, catalog, try_on_copy)
        except LLMUnavailable as error:
            log.warning("agent conclusion skipped error=%s", error)
        failing = failing_parts(executor)
        was_failing = bool(focus.failing)
        focus = focus.model_copy(
            update={
                "issue": focus.issue or asked,
                "plan": given or focus.plan,
                "failing": merged(focus.failing, failing),
            }
        )
        text = presenter.investigation(
            done, claims, cited, given, bool(focus.plan), failing, documents(done)
        )
        detail = f"{len(claims)} cited sentences · {len(given)} steps · health {health(failing)}"
        step = agent_step(state, "conclude", {"tool_calls": len(done)}, detail, started, llm_calls)
        if failing == [] and was_failing:
            written_up, wrote, focus = write_up(state, focus)
            return answer(state, f"{text}\n\n{written_up}", step, wrote) | {
                "work": [],
                "focus": focus,
            }
        return answer(state, text, step) | {"work": [], "focus": focus}

    def recheck(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        focus, failing = state.get("focus", Focus()), failing_parts(executor)
        step = agent_step(state, "recheck", {}, f"health {health(failing)}", started)
        if failing is None:
            return answer(state, "The sandbox is not reachable, so I cannot check it now.", step)
        if failing:
            # Still broken: the agent looks again with what this incident has learned.
            update = focus.model_copy(update={"failing": merged(focus.failing, failing)})
            return {"messages": step_messages(step), "trace": [step], "focus": update}
        if not (focus.issue or focus.failing):
            return answer(state, "Health is ok and nothing is failing.", step)
        text, wrote, focus = write_up(state, focus)
        return answer(state, text, step, wrote) | {"focus": focus}

    def fallback(state: AgentState, *before: Step) -> dict[str, Any]:
        # The model is not available: read-only checks and public pages, and no AI text.
        started = time.perf_counter()
        issue = state.get("focus", Focus()).issue or question(state)
        findings = triage(executor)
        evidence_text = " ".join([issue, *(f.output for f in findings if f.check != "logs")])
        refs = knowledge.references(evidence_text)
        checks = ", ".join(f.check for f in findings) or "sandbox not reachable"
        step = agent_step(
            state, "fallback", {"checks": checks}, f"{len(refs)} reference pages", started
        )
        uncovered = state["understanding"].intent == "suggest"
        return answer(state, presenter.fallback(refs, findings, uncovered), *before, step)

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
            text += f"\n\n{FOLLOW_UP}" if focus.issue else ""
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
        return answer(state, text, step) | {"focus": focus}

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
        return answer(state, reply_text, step) | {"focus": focus}

    def run_step(state: AgentState) -> dict[str, Any]:
        # Runs only because an engineer asked for this step: a check at once, a fix after the
        # engineer approves it.
        started = time.perf_counter()
        found, focus = state["understanding"], state.get("focus", Focus())
        section: Section = "rollback" if found.rollback else "steps"
        ordinal = found.step or 0
        if focus.plan and not focus.sop:
            text, detail = plan_step(focus.plan, ordinal)
            args: dict[str, str | int | float] = {"plan": "investigation", "step": ordinal}
        else:
            text, detail = sop_step(focus, section, ordinal, found.rollback)
            args = {"sop": focus.sop or "none", "section": section, "step": ordinal}
        step = Step(
            request_id=state["request_id"],
            node="run_step",
            args=args,
            detail=detail,
            ms=elapsed_ms(started),
        )
        return answer(state, text, step)

    def plan_step(plan: list[PlanStep], ordinal: int) -> tuple[str, str]:
        given = next((step for step in plan if step.ordinal == ordinal), None)
        if given is None:
            return f"The steps I gave have no step {ordinal}.", "no such step"
        known = {line: name for name, line in (catalog.checks | catalog.actions).items()}
        return perform(f"Step {ordinal}", given, known.get(given.command))

    def sop_step(focus: Focus, section: Section, ordinal: int, rollback: bool) -> tuple[str, str]:
        if not focus.sop or not focus.sop_version:
            return "Open an SOP first, for example: steps for db pool.", "no SOP in focus"
        stored = executor.step(focus.sop, focus.sop_version, section, ordinal)
        kind = "rollback step" if rollback else "step"
        if stored is None:
            return f"{focus.sop} v{focus.sop_version} has no {kind} {ordinal}.", "no such step"
        if not (stored.action or stored.check_name):
            return presenter.run_yourself(stored, "it has no sandbox check"), "manual step"
        title = f"{kind.capitalize()} {ordinal} of {focus.sop} v{focus.sop_version}"
        cited = f"{focus.sop} v{focus.sop_version}"
        # The stored step decides what runs, never the text of the chat.
        return perform(title, as_plan_step(stored, cited), stored.action or stored.check_name)

    def perform(title: str, step: PlanStep, name: str | None) -> tuple[str, str]:
        if name is None:
            return presenter.tried(title, step, None), "manual step"
        try:
            if name in catalog.checks:
                checked = executor.check(name)
                return presenter.tried(title, step, checked), outcome(checked)
            trial = executor.dry_run(name)
            if trial.met is None:
                return presenter.tried(title, step, trial), outcome(trial)
            # Pauses the run until the engineer approves or rejects it in the chat UI.
            ok, message = approved(interrupt(fix_request(name, step, trial)))
            if not ok:
                reason = f"rejected: {message}" if message else "rejected"
                return presenter.not_run(title, step, reason), "rejected"
            result = executor.act(name)
        except SandboxUnavailable as error:
            log.warning("step not run error=%s", error)
            return presenter.tried(title, step, None), "sandbox down"
        return presenter.ran(title, step, trial, result), outcome(result)

    def try_on_copy(name: str) -> StepResult | None:
        try:
            return executor.dry_run(name)
        except SandboxUnavailable as error:
            log.warning("dry run failed error=%s", error)
            return None

    def draft(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        # A sandbox that cannot be reached is not proof of a failure, so drafting goes ahead.
        failing = failing_parts(executor)
        if failing:
            text = (
                f"The system still reports a failure ({', '.join(failing)}). Fix it first,"
                " then ask me to draft the SOP and RCA."
            )
            step = agent_step(state, "draft", {}, "not healthy yet", started)
            return answer(state, text, step)
        text, wrote, focus = write_up(state, state.get("focus", Focus()))
        return answer(state, text, wrote) | {"focus": focus}

    def write_up(state: AgentState, focus: Focus) -> tuple[str, Step, Focus]:
        # The incident's memory becomes an SOP (a new version of the one followed, or a new
        # one) and an RCA, queued for a person to review.
        started = time.perf_counter()
        chat = transcript(state["messages"], state.get("trace", []))
        services = sorted(named_services(chat, catalog.services))
        alerts = [alert for alert, service in catalog.alerts.items() if service in services]
        followed = knowledge.document(focus.sop) if focus.sop else None
        llm_calls = 0
        try:
            with request_scope(state["request_id"]):
                written = writers.draft(draft_prompt(chat, services, alerts, followed))
            trace = state.get("trace", [])
            context = WriteUp(
                services,
                presenter.today(),
                followed,
                focus.rca,
                tuple(focus.drafts),
                trace[0].at if trace else None,
            )
            drafted = drafter.save(written, chat, context)
            text, llm_calls = presenter.drafted(drafted), 1
            detail = f"{drafted.sop_id} v{drafted.sop_version} and {drafted.rca_id} for review"
            focus = focus.model_copy(update={"drafts": drafted.review_ids})
        except LLMUnavailable as error:
            log.warning("draft skipped error=%s", error)
            text = (
                "Health is ok now, but the drafting model is not available, so nothing was"
                ' written up. Say "draft the SOP and RCA" later to try again.'
            )
            detail = "llm down"
        return text, agent_step(state, "draft", {}, detail, started, llm_calls), focus

    def record(state: AgentState) -> dict[str, Any]:
        # One row per turn for /ops: what was asked, the path taken, wall time and LLM calls.
        turn = [s for s in state.get("trace", []) if s.request_id == state["request_id"]]
        started = turn[0].at if turn else datetime.now(UTC)
        requests.record(
            Turn(
                request_id=state["request_id"],
                kind=state["understanding"].intent,
                path=" → ".join(step.node for step in turn),
                ms=round((datetime.now(UTC) - started).total_seconds() * 1000),
                llm_calls=sum(step.llm_calls for step in turn),
            )
        )
        return {}

    nodes: dict[str, Node] = {
        "understand": understand,
        "find": find,
        "suggest": suggest,
        "ask_sop": ask_sop,
        "ask_rca": ask_rca,
        "run_step": run_step,
        "draft": draft,
        "recheck": recheck,
        "troubleshoot": troubleshoot,
        "think": think,
        "tools": tools,
        "conclude": conclude,
        "reply": reply,
        "record": record,
    }
    builder = StateGraph(AgentState)
    builder.add_node("guard", guard_input)
    for name, node in nodes.items():
        builder.add_node(name, scoped(node))
    builder.add_edge(START, "guard")
    builder.add_edge("guard", "understand")
    builder.add_conditional_edges("understand", route, [*ROUTES, "reply"])
    for name in ("suggest", "recheck"):
        builder.add_conditional_edges(name, after_handoff, ["troubleshoot", "record"])
    for name in ("troubleshoot", "think"):
        builder.add_conditional_edges(name, after_think, ["tools", "conclude", "record"])
    builder.add_edge("tools", "think")
    for name in ("find", "ask_sop", "ask_rca", "run_step", "draft", "conclude", "reply"):
        builder.add_edge(name, "record")
    builder.add_edge("record", END)
    # With Langfuse keys set, every node run and the LLM calls inside it land in one trace.
    return builder.compile().with_config(callbacks=tracing)


def route(state: AgentState) -> str:
    found, focus = state["understanding"], state.get("focus", Focus())
    # A cause the library covers gets its SOP directly; the agent is for what it does not.
    if found.intent == "troubleshoot" and focus.sop:
        return "ask_sop"
    # In an investigation, a question that names nothing new is a follow-up for the agent.
    investigating = bool(focus.plan) and not focus.sop
    if found.intent in ("ask_sop", "unclear") and investigating and not topical(found):
        return "troubleshoot"
    # Unsure what is asked, but it names a service, error or alert and one reading is a
    # lookup: the closest documents serve every reading better than a question back. A
    # how-or-why split has no lookup reading and is still asked.
    lookups = "find" in found.alternatives or set(found.alternatives) <= {"find", "suggest"}
    if found.intent == "unclear" and lookups and topical(found):
        return "find"
    return found.intent if found.intent in ROUTES else "reply"


def scoped(node: Node) -> Node:
    # Every call a node makes, embeddings included, is logged under the turn's request ID.
    def run(state: AgentState) -> dict[str, Any]:
        with request_scope(state["request_id"]):
            return node(state)

    return run


def after_handoff(state: AgentState) -> str:
    return "record" if answered(state) else "troubleshoot"


def after_think(state: AgentState) -> str:
    if answered(state):
        return "record"
    return "tools" if pending(state["work"]) else "conclude"


def answered(state: AgentState) -> bool:
    # A turn ends with the reply; step cards end with a tool result.
    last = state["messages"][-1]
    return isinstance(last, AIMessage) and not last.tool_calls


def reply(state: AgentState) -> dict[str, Any]:
    found = state["understanding"]
    lines = [
        f"Understood as {found.intent} ({found.lane or 'no list'}),"
        f" confidence {found.confidence:.2f} by {found.method}."
    ]
    if found.alternatives:
        lines.append(f"Could also be: {', '.join(found.alternatives)}.")
    step = Step(request_id=state["request_id"], node="reply", args={}, detail="no route", ms=0)
    return answer(state, "\n".join(lines), step)


def topical(found: Understanding) -> bool:
    return bool(found.doc_id or found.services or found.errors or found.alerts)


def as_plan_step(step: SopStep, source: str) -> PlanStep:
    return PlanStep(
        ordinal=step.ordinal,
        instruction=step.instruction,
        command=step.command or "",
        expect=step.expect or "",
        changes=bool(step.action),
        source=source,
    )


def outcome(result: StepResult | None) -> str:
    if result is None:
        return "not tried"
    if result.kind == "check":
        return f"check {result.name} ran"
    if result.mode == "ran":
        return f"action {result.name} ran after approval · {result.note}"
    return f"action {result.name} tried first · {result.note}"


def fix_request(name: str, step: PlanStep, trial: StepResult) -> dict[str, Any]:
    target = "the shop on Kubernetes" if trial.mode == "cluster" else "the ShopLite sandbox"
    return approval_request(
        name,
        {"command": step.command},
        f"{step.instruction}\nCommand: {step.command}\nBefore running: {trial_note(trial)}.\n"
        f"Runs against {target}. Health is checked right after.",
    )


def approval_request(name: str, args: dict[str, Any], description: str) -> dict[str, Any]:
    # The HITLRequest shape agent-chat-ui shows as an approve or reject card. Edits are not
    # offered: what runs is the stored command, so an edited one would not be it.
    return {
        "action_requests": [{"name": name, "args": args, "description": description}],
        "review_configs": [{"action_name": name, "allowed_decisions": ["approve", "reject"]}],
    }


def approved(resume: Any) -> tuple[bool, str]:
    # The UI resumes with {"decisions": [{"type": "approve"}]}; anything else counts as no.
    decisions = resume.get("decisions") if isinstance(resume, dict) else None
    first = decisions[0] if isinstance(decisions, list) and decisions else None
    if not isinstance(first, dict):
        return False, ""
    return first.get("type") == "approve", str(first.get("message") or "")


def tool_result(
    call_id: str | None, name: str, output: str, run: ToolRun | None = None
) -> ToolMessage:
    return ToolMessage(
        content=output,
        tool_call_id=call_id or "",
        name=name,
        artifact=run.model_dump() if run else None,
    )


def agent_step(
    state: AgentState,
    node: str,
    args: dict[str, str | int | float],
    detail: str,
    started: float,
    llm_calls: int = 0,
) -> Step:
    return Step(
        request_id=state["request_id"],
        node=node,
        args={key: value for key, value in args.items() if value != ""},
        detail=detail,
        ms=elapsed_ms(started),
        llm_calls=llm_calls,
    )


def failing_parts(executor: Executor) -> list[str] | None:
    # None when the sandbox cannot be reached, so callers can tell unknown from healthy.
    try:
        health = executor.sandbox.health()
    except SandboxUnavailable:
        return None
    return sorted(part for part, state in health["components"].items() if state != "ok")


def merged(seen: list[str], failing: list[str] | None) -> list[str]:
    return sorted({*seen, *(failing or [])})


def health(failing: list[str] | None) -> str:
    return "unknown" if failing is None else ", ".join(failing) or "ok"


def answer(state: AgentState, text: str, *steps: Step) -> dict[str, Any]:
    turn = [s for s in state.get("trace", []) if s.request_id == state["request_id"]]
    content = "\n\n".join([text, *notices(state["guard"])]) + footer([*turn, *steps])
    messages = [message for s in steps for message in step_messages(s)]
    return {"messages": [*messages, AIMessage(content=content)], "trace": list(steps)}


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
    call_id = f"{step.node}-{uuid.uuid4().hex[:12]}"
    return [
        AIMessage(content="", tool_calls=[{"name": step.node, "args": step.args, "id": call_id}]),
        ToolMessage(
            content=f"{step.detail} · {step.ms:.0f} ms", tool_call_id=call_id, name=step.node
        ),
    ]


def question(state: AgentState) -> str:
    return next(m.text for m in reversed(state["messages"]) if isinstance(m, HumanMessage))


def shorten(text: str, limit: int = SHOWN_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def describe(report: GuardReport) -> str:
    parts = [f"cut to {MAX_BYTES // 1000} KB" if report.truncated else "size ok"]
    if report.redacted:
        parts.append(f"redacted {', '.join(report.redacted)}")
    if report.injection:
        parts.append(f"injection flagged: {', '.join(report.injection)}")
    return " · ".join(parts)
