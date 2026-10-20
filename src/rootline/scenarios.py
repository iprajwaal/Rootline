import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import psycopg
import yaml
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_sdk import get_sync_client

from rootline.agent.factory import default_graph
from rootline.config import Settings

SCENARIOS = Path("scenarios")
ORDER = ("s1", "s1b", "s2", "s3", "s4", "s5", "s6", "s7", "s8")
FOOTER = re.compile(r"\*steps: (.*?) · LLM calls (\d+) · (\d+) ms\*")
WAIT_S = 60
# Approved drafts are the only documents a run adds; removing them restores the corpus.
LEARNED = """
    select source_key, doc_id from documents
    where source_key like 'drafts/%%' and status <> 'deleted'
"""
NEW_DRAFTS = """
    select id from review_items
    where reason = 'draft' and status = 'open' and created_at >= %s order by id
"""


@dataclass
class Turn:
    reply: str
    llm_calls: int
    ms: int
    wall_ms: int


class Chat:
    # One conversation, either on the running agent server or, with no model, in-process.
    def __init__(self, stack: "Stack", no_model: bool, scratch: Path) -> None:
        self.stack = stack
        self.thread: str | None = None
        self.local = None
        if no_model:
            settings = stack.settings.model_copy(
                update={
                    "google_api_key": "",
                    "embed_api_key": "",
                    "llm_cache_dir": scratch / "llm",
                    "embed_cache_dir": scratch / "embeddings",
                }
            )
            self.local = default_graph(settings).copy(update={"checkpointer": InMemorySaver()})

    def say(self, text: str) -> Turn:
        started = time.perf_counter()
        if self.local is not None:
            state = self.local.invoke(
                {"messages": [HumanMessage(content=text)]},
                {"configurable": {"thread_id": "scenario"}},
            )
            reply = state["messages"][-1].text
        else:
            if self.thread is None:
                created = self.stack.agent.threads.create(metadata={"graph_id": "agent"})
                self.thread = created["thread_id"]
            state = self.stack.agent.runs.wait(
                self.thread, "agent", input={"messages": [{"role": "human", "content": text}]}
            )
            reply = state["messages"][-1]["content"]
        return turn(reply, started)

    def opened(self, thread: str, started: float) -> Turn:
        self.thread = thread
        state = self.stack.agent.threads.get_state(thread)
        return turn(state["values"]["messages"][-1]["content"], started)


@dataclass
class Stack:
    settings: Settings
    api: httpx.Client
    shop: httpx.Client
    agent: Any

    def reset(self) -> None:
        self.shop.post("/reset")
        # The watchdog resolves the open alert on its next tick.
        until(lambda: not self.api.get("/incidents").json(), "open alerts to resolve")

    def alert_chat(self, alert: str, chat: Chat) -> Turn:
        incident = until(lambda: self.incident(alert), f"alert {alert}")
        started = time.perf_counter()
        opened = self.api.post(f"/incidents/{incident['id']}/open", timeout=120)
        opened.raise_for_status()
        return chat.opened(opened.json()["thread_id"], started)

    def incident(self, alert: str) -> dict[str, Any] | None:
        found = [i for i in self.api.get("/incidents").json() if i["alert_name"] == alert]
        return found[0] if found and not found[0]["chat_opened"] else None

    def health(self) -> str:
        return "ok" if self.shop.get("/health").json()["status"] == "ok" else "failing"


def main() -> None:
    settings = Settings()
    names = sys.argv[1:] or list(ORDER)
    faults = yaml.safe_load(settings.catalog_path.read_text())["faults"]
    stack = Stack(
        settings,
        httpx.Client(base_url=settings.public_api_url, timeout=60),
        httpx.Client(base_url=settings.shoplite_url, timeout=30),
        get_sync_client(url=settings.agent_url),
    )
    results = []
    with TemporaryDirectory() as scratch:
        for name in names:
            spec = yaml.safe_load((SCENARIOS / f"{name}.yaml").read_text())
            results.append(run(name, spec, stack, faults, Path(scratch) / name))
            mark = "PASS" if results[-1]["passed"] else "FAIL"
            print(f"{mark} {name}: {spec['name']} ({results[-1]['seconds']}s)")
            for failure in results[-1]["failures"]:
                print(f"     {failure}")
    stack.reset()
    out = SCENARIOS / "runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(results, indent=2) + "\n")
    passed = sum(r["passed"] for r in results)
    print(f"{passed} of {len(results)} scenarios passed · {out}")
    sys.exit(0 if passed == len(results) else 1)


def run(
    name: str, spec: dict[str, Any], stack: Stack, faults: dict[str, Any], scratch: Path
) -> dict[str, Any]:
    began, started_at = time.perf_counter(), datetime.now(UTC)
    stack.reset()
    if spec.get("forget_learned"):
        forget_learned(stack)
    stack.shop.post(f"/chaos/{spec['fault']}")
    chat = Chat(stack, bool(spec.get("no_model")), scratch)
    failures: list[str] = []
    turns: list[dict[str, Any]] = []
    steps = list(spec["steps"])
    if spec["entry"] == "alert":
        opening = stack.alert_chat(faults[spec["fault"]]["alert"], chat)
        expect = steps.pop(0).get("expect", {}) if steps and "say" not in steps[0] else {}
        record(turns, failures, "(alert)", opening, expect)
    for step in steps:
        if "act" in step:
            # The engineer runs the fix; Rootline never does.
            stack.shop.post(f"/admin/actions/{step['act']}")
            turns.append({"engineer_ran": step["act"]})
            continue
        record(turns, failures, step["say"], chat.say(step["say"]), step.get("expect", {}))
    if spec.get("review"):
        review(stack, started_at, spec["review"], turns)
    if spec.get("health_after") and stack.health() != spec["health_after"]:
        failures.append(f"health is {stack.health()}, expected {spec['health_after']}")
    return {
        "scenario": name,
        "name": spec["name"],
        "proves": spec.get("proves", ""),
        "passed": not failures,
        "failures": failures,
        "turns": turns,
        "seconds": round(time.perf_counter() - began, 1),
    }


def record(
    turns: list[dict[str, Any]], failures: list[str], said: str, got: Turn, expect: dict[str, Any]
) -> None:
    missing = [text for text in expect.get("contains", []) if text not in got.reply]
    present = [text for text in expect.get("absent", []) if text in got.reply]
    problems = [f"missing {text!r}" for text in missing] + [f"shows {text!r}" for text in present]
    if "llm_calls_max" in expect and got.llm_calls > expect["llm_calls_max"]:
        problems.append(f"{got.llm_calls} LLM calls, at most {expect['llm_calls_max']}")
    if "max_ms" in expect and got.ms > expect["max_ms"]:
        problems.append(f"{got.ms} ms, at most {expect['max_ms']}")
    failures += [f"{said[:40]!r}: {problem}" for problem in problems]
    turns.append(
        {
            "said": said,
            "reply": got.reply.split("\n\n---\n")[0][:600],
            "llm_calls": got.llm_calls,
            "ms": got.ms,
            "wall_ms": got.wall_ms,
            "ok": not problems,
        }
    )


def review(stack: Stack, since: datetime, decision: str, turns: list[dict[str, Any]]) -> None:
    with psycopg.connect(stack.settings.database_url) as conn:
        drafts = [row[0] for row in conn.execute(NEW_DRAFTS, (since,))]
    for draft in drafts:
        stack.api.post(f"/review/{draft}/{decision}", follow_redirects=False)
    turns.append({"reviewer": decision, "drafts": drafts})


def forget_learned(stack: Stack) -> None:
    with psycopg.connect(stack.settings.database_url) as conn:
        learned = conn.execute(LEARNED).fetchall()
    for source_key, doc_id in learned:
        stack.api.delete("/documents", params={"source_key": source_key})
        print(f"     removed learned {doc_id} ({source_key}) so the scenario starts clean")


def turn(reply: str, started: float) -> Turn:
    footer = FOOTER.search(reply)
    return Turn(
        reply=reply,
        llm_calls=int(footer.group(2)) if footer else -1,
        ms=int(footer.group(3)) if footer else -1,
        wall_ms=round((time.perf_counter() - started) * 1000),
    )


def until(check: Any, what: str) -> Any:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if found := check():
            return found
        time.sleep(1)
    raise TimeoutError(f"waited {WAIT_S}s for {what}")


if __name__ == "__main__":
    main()
