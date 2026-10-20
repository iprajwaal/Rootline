import re
from datetime import date
from typing import Any

from rootline.agent.present import Presenter, checklist, code, footer
from rootline.agent.state import Step
from rootline.models import SopStep
from rootline.retrieval.search import Hit, Linked

PRESENTER = Presenter("http://rootline.test", 365, today=lambda: date(2026, 10, 20))


def hit(**changes: Any) -> Hit:
    fields: dict[str, Any] = {
        "doc_id": "SOP-DB-001",
        "doc_type": "sop",
        "title": "Recover orders-api from pool exhaustion",
        "status": "active",
        "origin": "greenfield",
        "verified": True,
        "needs_review": False,
        "version": 3,
        "effective_date": date(2026, 7, 14),
        "services": ["orders-api"],
        "score": 0.05,
        "similarity": 0.9,
        "evidence": ["matched: pool"],
        "snippet": "",
        "similar_copies": 0,
        "linked": [],
    }
    return Hit.model_validate(fields | changes)


def unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def test_checklist_shows_each_step_word_for_word_in_order() -> None:
    steps = [
        SopStep(ordinal=1, instruction="Raise DB_POOL_MAX *carefully*", command="kubectl set env"),
        SopStep(ordinal=2, instruction="Check [health]", expect="status <ok>"),
    ]

    lines = checklist(steps).splitlines()

    assert unescape(lines[0]) == "- [ ] **1.** Raise DB_POOL_MAX *carefully*  "
    assert lines[1] == "  Run: `kubectl set env`"
    assert unescape(lines[2]) == "- [ ] **2.** Check [health]  "
    assert unescape(lines[3]) == "  Expect: status <ok>"


def test_command_with_backticks_stays_one_code_span() -> None:
    assert code("echo `date`") == "`` echo `date` ``"
    assert code("`date`") == "`` `date` ``"


def test_card_labels_type_version_freshness_and_link() -> None:
    card = PRESENTER.card(hit())

    assert card.startswith("**\\[SOP\\] SOP-DB-001** Recover orders-api from pool exhaustion")
    assert "v3 · updated 2026-07-14 · orders-api" in card
    assert "[open](http://rootline.test/docs/SOP-DB-001)" in card


def test_old_document_is_shown_with_a_note_to_check_it() -> None:
    card = PRESENTER.card(hit(effective_date=date(2024, 1, 2)))

    assert "updated 2024-01-02, over 365 days old, check it still holds" in card


def test_card_flags_superseded_unreviewed_and_merged_copies() -> None:
    newer = Linked(doc_id="SOP-DB-002", doc_type="sop", title="New", relation="superseded_by")
    card = PRESENTER.card(
        hit(status="superseded", needs_review=True, similar_copies=1, linked=[newer])
    )

    assert "superseded by SOP-DB-002 · not reviewed yet · 1 similar copy merged" in card


def test_footer_lists_the_steps_llm_calls_and_time() -> None:
    steps = [
        Step(request_id="r", node="understand", args={}, detail="", ms=2, llm_calls=1),
        Step(request_id="r", node="ask_sop", args={}, detail="", ms=40),
    ]

    assert footer(steps).endswith("*steps: understand → ask\\_sop · LLM calls 1 · 42 ms*")
