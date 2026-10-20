from pathlib import Path

import pytest

from rootline.catalog import load_catalog
from rootline.core.models import ParsedDoc, RcaRecord, SopRecord, SopStep
from rootline.corpus.layout import load_document
from rootline.ingest.extract import AskRecord, Record, extract
from rootline.ingest.facts import extract_facts
from rootline.ingest.parsing import parse_file
from rootline.ingest.redaction import redact_doc
from rootline.llm.structured import LLMUnavailable

CORPUS = Path(__file__).parents[1] / "corpus"
GREENFIELD_SOURCES = sorted((CORPUS / "sources" / "greenfield").glob("*.yaml"))
RUNBOOK = "brownfield/orders-db-connections-runbook.txt"
OFFSETS = {"source_start", "source_end"}


def load(relative: str) -> ParsedDoc:
    doc, _ = redact_doc(parse_file(CORPUS / relative))
    return doc


def source(doc_id: str) -> Record:
    folder = (
        "greenfield"
        if (CORPUS / "sources" / "greenfield" / f"{doc_id}.yaml").exists()
        else "brownfield"
    )
    _, record = load_document(CORPUS / "sources" / folder / f"{doc_id}.yaml")
    return record


def db_pool_sop() -> SopRecord:
    record = source("sop-db-001")
    assert isinstance(record, SopRecord)
    return record


def with_steps(steps: list[SopStep]) -> SopRecord:
    numbered = [step.model_copy(update={"ordinal": n}) for n, step in enumerate(steps, start=1)]
    return db_pool_sop().model_copy(update={"steps": numbered, "rollback": []})


def fake_llm(record: Record) -> AskRecord:
    return lambda prompt, label: record


def without_offsets(record: Record) -> dict[str, object]:
    if isinstance(record, SopRecord):
        steps = {"__all__": OFFSETS}
        return record.model_dump(exclude={"steps": steps, "rollback": steps})
    return record.model_dump()


@pytest.mark.parametrize("path", GREENFIELD_SOURCES, ids=lambda path: path.stem)
def test_greenfield_extraction_matches_the_source_exactly(path: Path) -> None:
    meta, expected = load_document(path)
    rendered = next((CORPUS / "greenfield").glob(f"{path.stem}.*"))

    result = extract(load(f"greenfield/{rendered.name}"), meta.doc_type, from_template=True)

    assert result.method == "template" and result.verified
    assert result.record is not None
    assert without_offsets(result.record) == without_offsets(expected)


def test_greenfield_steps_get_offsets_in_source_order() -> None:
    result = extract(load("greenfield/sop-db-001.docx"), "sop", from_template=True)

    assert isinstance(result.record, SopRecord)
    starts = [s.source_start for s in result.record.steps if s.source_start is not None]
    assert len(starts) == len(result.record.steps)
    assert starts == sorted(starts)


def test_faithful_llm_extraction_is_verified() -> None:
    result = extract(load(RUNBOOK), "sop", False, fake_llm(with_steps(db_pool_sop().steps)))

    assert result.method == "llm"
    assert result.verified


def test_reordered_fake_extraction_is_rejected() -> None:
    steps = db_pool_sop().steps
    swapped = [steps[0], steps[2], steps[1], steps[3], steps[4]]

    result = extract(load(RUNBOOK), "sop", False, fake_llm(with_steps(swapped)))

    assert not result.verified
    assert "steps 3 is out of order" in result.issues


def test_invented_step_is_rejected() -> None:
    invented = SopStep(ordinal=6, instruction="Drop the orders table to clear the locks.")

    result = extract(
        load(RUNBOOK), "sop", False, fake_llm(with_steps([*db_pool_sop().steps, invented]))
    )

    assert not result.verified
    assert "steps 6 is not in the source" in result.issues


def test_changed_command_is_rejected() -> None:
    steps = db_pool_sop().steps
    changed = steps[3].model_copy(
        update={"command": "kubectl -n shop set env deploy/orders-api DB_POOL_MAX=500"}
    )

    result = extract(
        load(RUNBOOK), "sop", False, fake_llm(with_steps([*steps[:3], changed, steps[4]]))
    )

    assert not result.verified
    assert "steps 4 command does not match the source" in result.issues


def test_rca_with_invented_facts_is_not_verified() -> None:
    invented = RcaRecord(root_causes=["A solar flare flipped bits in the load balancer."])

    result = extract(
        load("brownfield/re-orders-api-503s-this-morning.eml"), "rca", False, fake_llm(invented)
    )

    assert not result.verified


def test_llm_outage_leaves_the_record_empty_and_unverified() -> None:
    def offline(prompt: str, label: str) -> Record:
        raise LLMUnavailable("offline")

    result = extract(load(RUNBOOK), "sop", False, offline)

    assert (result.record, result.method, result.verified) == (None, "none", False)


def test_reference_pages_have_no_record() -> None:
    result = extract(load("reference/ref-k8s-001.md"), "ref", from_template=True)

    assert result.record is None and result.verified


CATALOG = load_catalog(CORPUS / "catalog.yaml")


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("greenfield/sop-db-001.docx", "2026-07-14"),
        ("brownfield/orders-db-connections-runbook.txt", "2026-07-14"),
        ("brownfield/wiki-orders-api-db-pool.html", "2024-03-02"),
        ("brownfield/re-orders-api-503s-this-morning.eml", "2025-06-04"),
        ("brownfield/inc-2025-06-03-orders-db-pool.json", "2025-06-03"),
    ],
)
def test_effective_date_comes_from_the_best_available_source(relative: str, expected: str) -> None:
    assert extract_facts(load(relative), CATALOG).effective_date == expected


def test_template_services_and_ids_come_from_fields() -> None:
    facts = extract_facts(load("greenfield/sop-queue-002.docx"), CATALOG)

    assert facts.doc_id == "SOP-QUEUE-002"
    assert facts.services == ["orders-worker", "order-queue"]
    assert facts.supersedes == "SOP-QUEUE-001"


def test_half_filled_template_has_no_declared_id() -> None:
    assert extract_facts(load("brownfield/db-pool-sop-draft.md"), CATALOG).doc_id is None


def test_error_signatures_and_alerts_are_found() -> None:
    facts = extract_facts(load(RUNBOOK), CATALOG)

    assert (
        "psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec"
        in facts.error_signatures
    )
    assert facts.alerts == ["OrdersDbPoolExhausted"]
