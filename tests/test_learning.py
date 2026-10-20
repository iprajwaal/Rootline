from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fake_agent import Scripted, concluding, turn
from fake_shop import FakeShop
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_agent_graph import Chat, make_graph, reply

from rootline.agent.answer import Claim
from rootline.agent.drafts import Drafter, DraftLlm, StepDraft, transcript
from rootline.agent.knowledge import Knowledge
from rootline.agent.state import Step
from rootline.agent.troubleshoot import AskConclusion, StepLlm
from rootline.api.app import app, get_analyzer, get_settings
from rootline.catalog import Catalog
from rootline.config import Settings
from rootline.db.migrate import migrate
from rootline.ingest.analysis import Analyzer
from rootline.ingest.store import Store
from rootline.retrieval.references import references
from rootline.review import Draft, approve, drafts, reject

LEARNING_DB = "rootline_learning_test"
CORPUS = Path(__file__).parents[1] / "corpus"
DISK_ERROR = "OSError: [Errno 28] No space left on device: '/var/lib/shoplite/tmp/receipt-8812.pdf'"


@pytest.fixture(scope="module")
def learning_url(
    analyzer: Analyzer, offline_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    # The whole library, like production. The corpus has no internal document for a full
    # disk, so that issue starts with no SOP or RCA.
    admin_url = Settings().database_url
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"drop database if exists {LEARNING_DB} with (force)")
        admin.execute(f"create database {LEARNING_DB}")
    url = make_conninfo(admin_url, dbname=LEARNING_DB)
    with psycopg.connect(url, autocommit=True) as conn:
        migrate(conn)
        store = Store(conn, analyzer, tmp_path_factory.mktemp("raw"), offline_settings.embed_model)
        for folder in ("greenfield", "brownfield", "reference"):
            for path in sorted((CORPUS / folder).iterdir()):
                store.ingest(f"corpus/{folder}/{path.name}", path.read_bytes())
    yield url


@pytest.fixture
def conn(learning_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(learning_url, autocommit=True) as connection:
        yield connection


@pytest.fixture
def reviewer(
    conn: psycopg.Connection, analyzer: Analyzer, offline_settings: Settings, tmp_path: Path
) -> Iterator[Store]:
    # What a test approves is removed after it, so each scenario starts with no disk SOP.
    before = newest_review(conn)
    store = Store(conn, analyzer, tmp_path / "raw", offline_settings.embed_model, "reviewer")
    yield store
    for draft in drafts(conn):
        if draft.id > before and draft.status == "approved":
            store.delete(draft.source_key)


def disk_shop() -> FakeShop:
    return FakeShop(
        component="disk",
        fault="disk_full",
        fix="clear_tmp",
        outputs={
            "disk_usage": "/var/lib/shoplite 100% used",
            "logs": f"ERROR [orders-api] {DISK_ERROR}",
        },
    )


def newest_review(conn: psycopg.Connection) -> int:
    row = conn.execute("select coalesce(max(id), 0) from review_items").fetchone()
    return int(row[0]) if row else 0


def drafted_since(conn: psycopg.Connection, since: int) -> list[Draft]:
    return [d for d in drafts(conn) if d.id > since and d.status == "open"]


def draft_writer(analyzer: Analyzer) -> Any:
    catalog = analyzer.catalog

    def write(prompt: str) -> DraftLlm:
        return DraftLlm(
            topic="disk",
            sop_title="Free disk space on orders-api",
            purpose="Recover orders-api when its temp folder fills the disk.",
            when_to_use="Writes fail with No space left on device.",
            alert="OrdersApiDiskFull",
            steps=[
                StepDraft(
                    instruction="Check how full the disk is.",
                    command=catalog.checks["disk_usage"],
                    expect="Usage near 100%.",
                ),
                StepDraft(
                    instruction="Delete temp files older than a day.",
                    command=catalog.actions["clear_tmp"],
                    expect="Usage drops below 80%.",
                ),
                StepDraft(instruction="Wipe the node.", command="rm -rf /", expect="Nothing."),
            ],
            verification=["Orders load again."],
            rca_title="Orders API writes failed on a full disk",
            short_description="Receipts filled /var/lib/shoplite/tmp.",
            problem_statement=f"Writes failed with {DISK_ERROR}",
            impact="Receipt downloads failed.",
            trigger="Old receipt files were never cleaned up.",
            immediate_resolution="Deleted temp files older than a day.",
            root_causes=["Nothing removed old files from the temp folder."],
            timeline=[],
            action_items=["Add a nightly cleanup job."],
        )

    return write


def pool_writer(catalog: Catalog, prompts: list[str]) -> Any:
    def write(prompt: str) -> DraftLlm:
        prompts.append(prompt)
        return DraftLlm(
            topic="DB",
            sop_title="ignored for an existing SOP",
            purpose="Recover orders-api from database connection pool exhaustion.",
            when_to_use="OrdersDbPoolExhausted fires.",
            alert="OrdersDbPoolExhausted",
            prerequisites=["kubectl access to the shop namespace"],
            steps=[
                StepDraft(
                    instruction="Count open connections.",
                    command=catalog.checks["db_connections"],
                    expect="100 of 100.",
                ),
                StepDraft(
                    instruction="Raise the pool limit.",
                    command=catalog.actions["raise_pool_limit"],
                    expect="Pool timeouts stop.",
                ),
            ],
            verification=["No PoolTimeout in the logs."],
            sop_change="Worked as written.",
            rca_title="Orders API ran out of database connections again",
            short_description="The pool filled during peak traffic.",
            problem_statement="psycopg_pool.PoolTimeout on orders-api.",
            impact="Checkout failed for some customers.",
            trigger="Traffic peak.",
            immediate_resolution="Raised DB_POOL_MAX to 50.",
            root_causes=["The pool limit was too low for peak traffic."],
            timeline=[],
            action_items=["Alert at 80% pool use."],
        )

    return write


def chat_for(
    analyzer: Analyzer,
    url: str,
    settings: Settings,
    shop: FakeShop,
    agent: Scripted | None = None,
    conclusion: AskConclusion | None = None,
) -> Chat:
    # Without a scripted agent the model is unavailable, as in a run with no API key.
    graph = make_graph(
        analyzer,
        url,
        settings,
        shop=shop,
        ask_draft=draft_writer(analyzer),
        ask_agent=agent,
        ask_conclusion=conclusion,
    )
    return Chat(graph)


def test_reference_pages_match_on_rare_words_not_on_wording(conn: psycopg.Connection) -> None:
    disk = references(conn, f"{DISK_ERROR} disk /var/lib/shoplite 100% used")
    bread = references(conn, "how do I bake sourdough bread")

    assert {"REF-PG-001", "REF-K8S-001"} <= {ref.doc_id for ref in disk}
    assert all(ref.licence and ref.source_url for ref in disk)
    assert bread == []


def test_without_the_llm_the_fallback_still_shows_references_and_checks(
    analyzer: Analyzer, learning_url: str, offline_settings: Settings
) -> None:
    chat = chat_for(analyzer, learning_url, offline_settings, disk_shop())

    state = chat.say(DISK_ERROR)
    text = reply(state)

    assert text.startswith("No internal SOP or RCA covers this.")
    assert "The assistant model is not available" in text
    assert "**\\[REFERENCE · external\\]**" in text and "REF-PG-001" in text
    assert "- disk\\_usage: /var/lib/shoplite 100% used" in text
    assert "AI ·" not in text
    assert [step.node for step in state["trace"]][-3:] == ["suggest", "think", "fallback"]


def test_drafting_waits_until_the_system_is_healthy(
    analyzer: Analyzer, learning_url: str, offline_settings: Settings
) -> None:
    chat = chat_for(analyzer, learning_url, offline_settings, disk_shop())
    chat.say(DISK_ERROR)

    text = reply(chat.say("draft the SOP and RCA"))

    assert text.startswith("The system still reports a failure (disk).")


def test_scenario_three_then_four_the_new_sop_is_found(
    analyzer: Analyzer,
    learning_url: str,
    offline_settings: Settings,
    conn: psycopg.Connection,
    reviewer: Store,
) -> None:
    catalog = analyzer.catalog
    shop = disk_shop()
    chat = chat_for(analyzer, learning_url, offline_settings, shop)
    chat.say(DISK_ERROR)
    shop.broken = False
    before = newest_review(conn)

    text = reply(
        chat.say(
            f"Fixed it. I ran `{catalog.checks['disk_usage']}`"
            f" and then `{catalog.actions['clear_tmp']}`."
        )
    )
    queued = drafted_since(conn, before)

    assert text.startswith("Health is ok now, so I wrote up the incident from this chat.")
    assert "Left out commands that never appeared in this chat: `rm -rf /`" in text
    sop = next(d for d in queued if d.doc_type == "sop")
    assert sop.doc_id.startswith("SOP-DISK-") and f"{sop.doc_id} (new)" in text
    assert catalog.actions["clear_tmp"] in sop.markdown and "rm -rf" not in sop.markdown
    assert Knowledge(learning_url, analyzer.embedder, catalog).search(sop.doc_id).sop.hits == []

    for draft in queued:
        assert approve(conn, reviewer, draft.id)

    found = Knowledge(learning_url, analyzer.embedder, catalog).suggest(
        f"[FIRING:1] OrdersApiDiskFull orders-api\n{DISK_ERROR}"
    )
    [top] = found.sop.suggestions[:1]
    assert top.doc_id == sop.doc_id
    assert "same alert OrdersApiDiskFull" in top.why
    assert found.latency_ms < 1000


def test_scenario_three_then_four_from_the_agents_steps(
    analyzer: Analyzer,
    learning_url: str,
    offline_settings: Settings,
    conn: psycopg.Connection,
    reviewer: Store,
) -> None:
    catalog = analyzer.catalog
    agent = Scripted(
        [
            turn("", ("check_health", {})),
            turn("", ("run_check", {"name": "disk_usage"})),
            turn("", ("search_references", {"query": "No space left on device disk full"})),
            turn("", ("try_fix", {"name": "clear_tmp"})),
        ]
    )
    conclusion = concluding(
        [Claim(text="The disk at /var/lib/shoplite is 100% used.", sources=[2])],
        StepLlm(instruction="Confirm the disk is full.", command=catalog.checks["disk_usage"]),
        StepLlm(instruction="Delete old temp files.", command=catalog.actions["clear_tmp"]),
    )
    shop = disk_shop()
    chat = chat_for(analyzer, learning_url, offline_settings, shop, agent, conclusion)
    advised = reply(chat.say(DISK_ERROR))
    shop.broken = False
    before = newest_review(conn)

    done = reply(chat.say("done"))
    sop = next(d for d in drafted_since(conn, before) if d.doc_type == "sop")

    assert "**Steps to follow**" in advised and "REF-" in advised and shop.actions() == []
    assert done.startswith("Health is ok now, so I wrote up the incident from this chat.")
    assert catalog.actions["clear_tmp"] in sop.markdown
    assert catalog.checks["disk_usage"] in sop.markdown
    for draft in drafted_since(conn, before):
        assert approve(conn, reviewer, draft.id)
    suggested = Knowledge(learning_url, analyzer.embedder, catalog).suggest(DISK_ERROR)
    assert suggested.sop.suggestions[0].doc_id == sop.doc_id


def test_following_an_existing_sop_writes_its_next_version_and_a_new_rca(
    analyzer: Analyzer,
    learning_url: str,
    offline_settings: Settings,
    conn: psycopg.Connection,
    reviewer: Store,
) -> None:
    prompts: list[str] = []
    shop = FakeShop()
    graph = make_graph(
        analyzer,
        learning_url,
        offline_settings,
        shop=shop,
        ask_draft=pool_writer(analyzer.catalog, prompts),
    )
    chat = Chat(graph)
    chat.say("[FIRING:1] OrdersDbPoolExhausted orders-api")
    shop.broken = False
    before = newest_review(conn)

    text = reply(chat.say("done, it works now"))
    sop, rca = sorted(drafted_since(conn, before), key=lambda d: d.doc_type, reverse=True)

    assert "The engineer followed the existing SOP below." in prompts[0]
    assert "SOP-DB-001 (update to v2)" in text and f"{rca.doc_id} (new)" in text
    assert sop.doc_id == "SOP-DB-001" and "kubectl access to the shop namespace" in sop.markdown
    assert "Worked as written." in sop.markdown and "RCA-2025-014" in rca.markdown
    assert "built on [\\[SOP\\] SOP-DB-001 v1](http://rootline.test/docs/SOP-DB-001)" in text
    assert "related to [\\[RCA\\] RCA-2025-014](http://rootline.test/docs/RCA-2025-014)" in text
    assert approve(conn, reviewer, sop.id) and approve(conn, reviewer, rca.id)
    version = "select current_version from documents where doc_id = 'SOP-DB-001'"
    assert conn.execute(version).fetchone() == (2,)
    # The seed reads the corpus again on every start; that must not undo the reviewed version.
    source = CORPUS / "greenfield" / "sop-db-001.docx"
    again = reviewer.ingest(f"corpus/greenfield/{source.name}", source.read_bytes())
    assert again.action == "unchanged" and conn.execute(version).fetchone() == (2,)


def test_approving_twice_ingests_once_and_rejecting_never_indexes(
    analyzer: Analyzer,
    learning_url: str,
    offline_settings: Settings,
    conn: psycopg.Connection,
    reviewer: Store,
) -> None:
    shop = disk_shop()
    shop.broken = False
    chat = chat_for(analyzer, learning_url, offline_settings, shop)
    chat.say(DISK_ERROR)
    before = newest_review(conn)
    chat.say("draft the SOP and RCA")
    sop, rca = sorted(drafted_since(conn, before), key=lambda d: d.doc_type, reverse=True)

    assert approve(conn, reviewer, sop.id) and approve(conn, reviewer, sop.id) is None
    assert reject(conn, rca.id) and not reject(conn, rca.id)
    assert (
        conn.execute("select 1 from documents where doc_id = %s", (rca.doc_id,)).fetchone() is None
    )


def test_review_page_lists_drafts_and_approval_redirects_back(
    analyzer: Analyzer,
    learning_url: str,
    offline_settings: Settings,
    conn: psycopg.Connection,
    reviewer: Store,
) -> None:
    shop = disk_shop()
    shop.broken = False
    chat = chat_for(analyzer, learning_url, offline_settings, shop)
    chat.say(DISK_ERROR)
    before = newest_review(conn)
    chat.say("draft the SOP and RCA")
    draft = drafted_since(conn, before)[0]
    settings = offline_settings.model_copy(update={"database_url": learning_url})
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    client = TestClient(app)

    page = client.get("/review")
    approved = client.post(f"/review/{draft.id}/approve", follow_redirects=False)
    again = client.post(f"/review/{draft.id}/approve", follow_redirects=False)
    app.dependency_overrides.clear()

    assert page.status_code == 200 and draft.doc_id in page.text
    assert "<script" not in page.text
    assert (approved.status_code, approved.headers["location"]) == (303, "/review")
    assert again.status_code == 404


def test_transcript_stamps_each_turn_and_drops_footers() -> None:
    step = Step(request_id="r1", node="guard", args={}, detail="", ms=0)
    messages: list[AnyMessage] = [
        HumanMessage(content="disk is full"),
        ToolMessage(content="size ok · 0 ms", tool_call_id="guard-r1", name="guard"),
        AIMessage(content="No internal SOP or RCA covers this.\n\n---\n*steps: guard · 1 ms*"),
    ]

    text = transcript(messages, [step])

    stamp = step.at.strftime("%H:%M:%S UTC")
    assert text.splitlines() == [
        f"[{stamp}] engineer: disk is full",
        f"[{stamp}] step guard: size ok · 0 ms",
        f"[{stamp}] rootline: No internal SOP or RCA covers this.",
    ]


def test_drafted_ids_follow_on_from_documents_and_waiting_drafts(
    conn: psycopg.Connection, analyzer: Analyzer, learning_url: str
) -> None:
    drafter = Drafter(learning_url, analyzer.catalog)
    first = drafter.next_id(conn, "SOP-QUOTA-")
    conn.execute(
        "insert into review_items (reason, details) values ('draft', %s)",
        (Jsonb({"doc_id": "SOP-QUOTA-004"}),),
    )

    assert (first, drafter.next_id(conn, "SOP-QUOTA-")) == ("SOP-QUOTA-001", "SOP-QUOTA-005")
