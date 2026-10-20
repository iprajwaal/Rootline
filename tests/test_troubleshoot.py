from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from fake_agent import Scripted, concluding, turn
from fake_shop import FakeShop
from test_agent_graph import Chat, make_graph, reply

from rootline.agent.answer import Claim
from rootline.agent.present import FOLLOW_UP
from rootline.agent.troubleshoot import AskConclusion, StepLlm
from rootline.catalog import Catalog
from rootline.config import Settings
from rootline.ingest.analysis import Analyzer

DISK_ERROR = "OSError: [Errno 28] No space left on device: '/var/lib/shoplite/tmp/receipt-8812.pdf'"
Call = tuple[str, dict[str, str]]
HEALTH: Call = ("check_health", {})
DISK: Call = ("run_check", {"name": "disk_usage"})
TRY: Call = ("try_fix", {"name": "clear_tmp"})
SOP_LINK = "[\\[SOP\\] SOP-DB-001 v1](http://rootline.test/docs/SOP-DB-001)"

Make = Callable[..., Chat]


def disk_shop() -> FakeShop:
    return FakeShop(
        component="disk",
        fault="disk_full",
        fix="clear_tmp",
        outputs={"disk_usage": "/var/lib/shoplite 100% used", "logs": f"ERROR {DISK_ERROR}"},
    )


def investigation() -> list[Any]:
    return [
        turn("Check health first.", HEALTH),
        turn("Disk is failing, so check how full it is.", DISK),
        turn("It is 100% used; see whether clearing old temp files would fix it.", TRY),
    ]


def findings(catalog: Catalog) -> AskConclusion:
    return concluding(
        [
            Claim(text="The disk at /var/lib/shoplite is 100% used.", sources=[2]),
            Claim(text="Cosmic rays flipped a bit in the kernel.", sources=[1]),
        ],
        StepLlm(
            instruction="Confirm the disk is full.",
            command=catalog.checks["disk_usage"],
            expect="Usage near 100%.",
        ),
        StepLlm(
            instruction="Delete temp files older than a day.",
            command=catalog.actions["clear_tmp"],
            expect="Usage drops below 80%.",
        ),
        StepLlm(instruction="Restart to be safe.", command=catalog.actions["restart_api"]),
        StepLlm(instruction="Wipe the node.", command="rm -rf /"),
        StepLlm(
            instruction="Confirm orders load again.",
            command=catalog.checks["health"],
            expect="status ok",
        ),
    )


@pytest.fixture
def make_chat(
    search_db: psycopg.Connection,
    search_db_url: str,
    analyzer: Analyzer,
    offline_settings: Settings,
) -> Make:
    def make(shop: FakeShop, agent: Scripted) -> Chat:
        graph = make_graph(
            analyzer,
            search_db_url,
            offline_settings,
            shop=shop,
            ask_agent=agent,
            ask_conclusion=findings(analyzer.catalog),
        )
        return Chat(graph)

    return make


def nodes(state: dict[str, Any]) -> list[str]:
    turn_id = state["request_id"]
    return [step.node for step in state["trace"] if step.request_id == turn_id]


def test_issue_no_document_covers_gets_a_tested_breakdown_and_nothing_changes(
    make_chat: Make, analyzer: Analyzer
) -> None:
    shop = disk_shop()
    chat = make_chat(shop, Scripted(investigation()))

    state = chat.say(DISK_ERROR)
    text = reply(state)
    planned = text.split("**Steps to follow**")[1]

    assert "__interrupt__" not in state
    assert shop.actions() == [] and shop.broken
    assert nodes(state) == [
        "guard", "understand", "suggest", "think", "check_health", "think", "run_check",
        "think", "try_fix", "think", "conclude",
    ]  # fmt: skip
    assert "The disk at /var/lib/shoplite is 100% used. \\[2\\]" in text
    assert "Cosmic rays" not in text
    assert analyzer.catalog.actions["clear_tmp"] in planned
    assert "Changes the system. Tried on a copy: fixes disk\\_full, health ok after it." in planned
    assert "restart" not in planned and "rm -rf" not in text
    assert [step.ordinal for step in state["focus"].plan] == [1, 2, 3]
    assert FOLLOW_UP in text and "Still failing: disk." in text
    # The agent did not search, but the library was searched before it was handed the issue.
    assert "No internal SOP or RCA covers this." in text
    assert "**What I looked at** (nothing was changed)" in text


def test_running_a_step_runs_its_check_and_asks_before_a_change(make_chat: Make) -> None:
    shop = disk_shop()
    chat = make_chat(shop, Scripted(investigation()))
    chat.say(DISK_ERROR)

    check = reply(chat.say("run step 1"))
    paused = chat.say("run step 2")
    fixed = reply(chat.resume({"type": "approve"}))

    assert check.startswith("**Step 1** Confirm the disk is full.")
    assert "/var/lib/shoplite 100% used" in check
    assert paused["__interrupt__"][0].value["action_requests"][0]["name"] == "clear_tmp"
    assert "**Health is ok again.**" in fixed
    assert "Should I write this incident up as a new SOP and RCA?" in fixed
    assert shop.actions() == ["POST /admin/actions/clear_tmp"] and not shop.broken


def test_a_question_about_a_step_goes_back_to_the_agent_with_the_steps(make_chat: Make) -> None:
    agent = Scripted(investigation(), [turn("Look at the disk again.", DISK)])
    chat = make_chat(disk_shop(), agent)
    chat.say(DISK_ERROR)

    state = chat.say("what does step 2 do?")

    assert nodes(state)[2:] == ["think", "run_check", "think", "conclude"]
    task = agent.seen[-1][1].text
    assert "Steps given to the engineer so far:\n1. Confirm the disk is full." in task
    assert "Parts seen failing so far: disk." in task


def test_done_while_still_failing_sends_the_agent_back_to_look(make_chat: Make) -> None:
    agent = Scripted(investigation(), [turn("Still failing; check the disk.", DISK)])
    chat = make_chat(disk_shop(), agent)
    chat.say(DISK_ERROR)

    state = chat.say("done")

    assert nodes(state)[2:5] == ["recheck", "think", "run_check"]
    assert "Still failing: disk." in reply(state)


def test_the_agent_stops_after_six_tool_calls(make_chat: Make) -> None:
    chat = make_chat(disk_shop(), Scripted([turn("", HEALTH) for _ in range(10)]))

    state = chat.say(DISK_ERROR)

    assert nodes(state).count("check_health") == 6
    assert any(s.detail == "stopped: all 6 tool calls used" for s in state["trace"])


def test_a_known_cause_gets_its_sop_directly_without_the_agent(make_chat: Make) -> None:
    agent = Scripted(investigation())
    shop = FakeShop()
    chat = make_chat(shop, agent)

    suggested = reply(chat.say("[FIRING:1] OrdersDbPoolExhausted orders-api"))
    state = chat.say("can you give me commands to solve this now?")

    assert SOP_LINK.join(["**Steps from** ", ""]) in suggested and FOLLOW_UP in suggested
    assert state["understanding"].intent == "troubleshoot"
    assert reply(state).startswith("**\\[SOP\\] SOP-DB-001 · v1**")
    assert agent.seen == [] and shop.actions() == []


def test_an_sop_the_agent_reads_is_cited_and_linked(
    search_db: psycopg.Connection,
    search_db_url: str,
    analyzer: Analyzer,
    offline_settings: Settings,
) -> None:
    catalog = analyzer.catalog
    agent = Scripted(
        [
            turn("", ("search_docs", {"query": "pool timeout"})),
            turn("", ("read_sop", {"doc_id": "SOP-DB-001"})),
        ]
    )
    conclusion = concluding(
        [Claim(text="Recover orders-api from database connection pool exhaustion.", sources=[2])],
        StepLlm(instruction="Count connections.", command=catalog.checks["db_connections"]),
    )
    graph = make_graph(
        analyzer,
        search_db_url,
        offline_settings,
        shop=FakeShop(),
        ask_agent=agent,
        ask_conclusion=conclusion,
    )

    text = reply(Chat(graph).say("fix it"))

    assert "[\\[2\\]](http://rootline.test/docs/SOP-DB-001)" in text
    assert (
        "**Documents I checked**\n- [\\[SOP\\] SOP-DB-001](http://rootline.test/docs/SOP-DB-001)"
        in text
    )
    assert "From [\\[SOP\\] SOP-DB-001](http://rootline.test/docs/SOP-DB-001)" in text
