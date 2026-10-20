from pathlib import Path

import pytest

from rootline.core.models import Classification, ParsedDoc
from rootline.ingest.classify import LlmVerdict, classify
from rootline.ingest.document_builder import DocBuilder
from rootline.ingest.learned import Example, labelled_examples, rule_features, train
from rootline.ingest.parsing import parse_file
from rootline.ingest.redaction import redact_doc

CORPUS = Path(__file__).parents[1] / "corpus"


@pytest.fixture(scope="module")
def examples() -> list[Example]:
    return labelled_examples(CORPUS)


def text_doc(*lines: str) -> ParsedDoc:
    builder = DocBuilder()
    for line in lines:
        builder.add("paragraph", line)
    return builder.build("notes.txt", "txt", {})


class Fixed:
    def __init__(self, label: str, confidence: float) -> None:
        self.result = Classification.model_validate(
            {"label": label, "confidence": confidence, "method": "lr"}
        )

    def classify(self, doc: ParsedDoc) -> Classification:
        return self.result


def test_training_set_has_both_types_and_leaves_the_hard_set_out(
    examples: list[Example],
) -> None:
    assert {e.label for e in examples} == {"rca", "sop"}
    assert not any(e.source.startswith("hard/") for e in examples)


def test_model_labels_a_copy_it_never_saw(examples: list[Example]) -> None:
    learned = train([e for e in examples if "SOP-DB-001" not in e.group])
    doc, _ = redact_doc(parse_file(CORPUS / "brownfield" / "orders-db-connections-runbook.txt"))

    result = learned.classify(doc)

    assert (result.label, result.method) == ("sop", "lr")


def test_rule_features_count_steps_and_timestamps() -> None:
    _, steps, stamps = rule_features(
        text_doc("1. Run kubectl rollout restart", "2025-10-21 02:11 cache pod evicted")
    )
    _, no_steps, no_stamps = rule_features(text_doc("The cache was slow", "Nobody knew why"))

    assert steps > 0 and stamps > 0
    assert no_steps == no_stamps == 0


def test_confident_first_guess_skips_the_llm() -> None:
    def never(prompt: str) -> LlmVerdict:
        raise AssertionError("the LLM should not be asked")

    doc = text_doc("1. Run kubectl rollout restart and check the pods")

    assert classify(doc, never, Fixed("sop", 0.9)).method == "lr"


def test_unsure_first_guess_asks_the_llm_and_needs_it_to_agree() -> None:
    def answer(prompt: str) -> LlmVerdict:
        return LlmVerdict(label="rca", confidence=0.95, reason="reads like an incident")

    doc = text_doc("1. Run kubectl rollout restart and check the pods")
    result = classify(doc, answer, Fixed("sop", 0.55))

    assert (result.label, result.method) == ("rca", "llm")
    assert result.confidence < 0.7
    assert "lr said sop" in result.evidence[-1]
