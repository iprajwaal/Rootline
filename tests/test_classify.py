import json
from pathlib import Path
from typing import Any

import pytest

from rootline.core.models import ParsedDoc
from rootline.ingest.classify import (
    LlmVerdict,
    classify,
    needs_review,
    split_mixed,
)
from rootline.ingest.document_builder import DocBuilder
from rootline.ingest.parsing import parse_file
from rootline.ingest.redaction import redact_doc
from rootline.llm.structured import LLMUnavailable

CORPUS = Path(__file__).parents[1] / "corpus"


def truth(group: str) -> list[dict[str, Any]]:
    path = CORPUS / "ground_truth" / f"{group}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def load(relative: str) -> ParsedDoc:
    doc, _ = redact_doc(parse_file(CORPUS / relative))
    return doc


def verdict(label: str, confidence: float = 0.9) -> LlmVerdict:
    return LlmVerdict.model_validate({"label": label, "confidence": confidence, "reason": "fake"})


def unavailable(prompt: str) -> LlmVerdict:
    raise LLMUnavailable("offline")


TEMPLATE_DOCS = [
    *(("greenfield/" + p.name, p.name[:3]) for p in sorted((CORPUS / "greenfield").iterdir())),
    *(("reference/" + p.name, "ref") for p in sorted((CORPUS / "reference").iterdir())),
]
NON_TEMPLATE_ROWS = truth("brownfield") + truth("hard")


@pytest.mark.parametrize(("relative", "label"), TEMPLATE_DOCS, ids=[d[0] for d in TEMPLATE_DOCS])
def test_template_documents_are_labelled_from_structure(relative: str, label: str) -> None:
    result = classify(load(relative))

    assert (result.label, result.method, result.confidence) == (label, "template", 1.0)


@pytest.mark.parametrize("row", NON_TEMPLATE_ROWS, ids=lambda row: row["file"])
def test_rules_are_never_confidently_wrong(row: dict[str, Any]) -> None:
    result = classify(load(row["file"]))

    assert result.label == row["doc_type"] or needs_review(result)


@pytest.mark.parametrize(
    "row", [r for r in truth("brownfield") if r["misleading_title"]], ids=lambda r: r["file"]
)
def test_misleading_titles_do_not_decide_the_label(row: dict[str, Any]) -> None:
    assert classify(load(row["file"])).label == row["doc_type"]


@pytest.mark.parametrize(
    "row", [r for r in truth("brownfield") if r["doc_type"] == "mixed"], ids=lambda r: r["file"]
)
def test_mixed_document_splits_into_incident_and_procedure(row: dict[str, Any]) -> None:
    doc = load(row["file"])

    rca, sop = split_mixed(doc)

    assert classify(doc).label == "mixed"
    assert (rca.source_key, sop.source_key) == (f"{doc.source_key}#rca", f"{doc.source_key}#sop")
    assert "Timeline" in rca.text and "Timeline" not in sop.text
    assert "Steps" in sop.text and "Steps" not in rca.text


def test_unsure_document_goes_to_the_llm() -> None:
    result = classify(load("hard/hard-01.txt"), lambda prompt: verdict("rca"))

    assert result.method == "llm"
    assert result.label == "rca"


def test_llm_label_without_rule_support_is_sent_to_review() -> None:
    result = classify(load("hard/hard-05.txt"), lambda prompt: verdict("mixed", 0.95))

    assert result.label == "mixed"
    assert needs_review(result)


def test_llm_label_that_agrees_with_rules_is_trusted() -> None:
    builder = DocBuilder()
    builder.add("paragraph", "Check the cache pod, then restart it and confirm latency recovers.")
    builder.add("paragraph", "The outage was bad and customers were affected for an hour.")
    builder.add("paragraph", "Run kubectl get pods to check.")
    doc = builder.build("note.txt", "txt", {})

    result = classify(doc, lambda prompt: verdict("sop", 0.9))

    assert (result.label, result.confidence) == ("sop", 0.9)
    assert not needs_review(result)


def test_llm_outage_keeps_rule_result_and_flags_review() -> None:
    result = classify(load("hard/hard-01.txt"), unavailable)

    assert result.method == "rules"
    assert needs_review(result)
