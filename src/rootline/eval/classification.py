import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rootline.config import Settings
from rootline.core.models import Classification, ParsedDoc
from rootline.ingest.classify import (
    AskLlm,
    LlmClassifier,
    classify,
    needs_review,
    template_label,
)
from rootline.ingest.factory import label_asker
from rootline.ingest.learned import Example, LogisticClassifier, labelled_examples, train
from rootline.ingest.parsing import parse_file
from rootline.ingest.redaction import redact_doc
from rootline.llm.ledger import PostgresLedger
from rootline.llm.structured import build_llm

CORPUS = Path("corpus")
OUTPUT = Path("eval/runs/classification.jsonl")
MODES = ("rules", "lr", "llm", "cascade", "lr_cascade")


def truth_labels(corpus: Path) -> dict[str, tuple[str, str]]:
    labels = {}
    for path in sorted((corpus / "greenfield").iterdir()):
        labels[f"greenfield/{path.name}"] = ("greenfield", path.name[:3])
    for group in ("brownfield", "hard"):
        for line in (corpus / "ground_truth" / f"{group}.jsonl").read_text().splitlines():
            row = json.loads(line)
            labels[row["file"]] = (group, row["doc_type"])
    for path in sorted((corpus / "reference").iterdir()):
        labels[f"reference/{path.name}"] = ("reference", "ref")
    return labels


def source_groups(corpus: Path) -> dict[str, frozenset[str]]:
    groups = {
        f"greenfield/{path.name}": frozenset([path.stem.upper()])
        for path in sorted((corpus / "greenfield").iterdir())
    }
    for name in ("brownfield", "hard"):
        for line in (corpus / "ground_truth" / f"{name}.jsonl").read_text().splitlines():
            row = json.loads(line)
            groups[row["file"]] = frozenset(row.get("source_ids", [row["file"]]))
    return groups


@dataclass
class HeldOut:
    # A document is judged by a model trained without it and without any copy of it.
    examples: list[Example]
    groups: dict[str, frozenset[str]]
    models: dict[frozenset[str], LogisticClassifier] = field(default_factory=dict)

    def model_for(self, relative: str) -> LogisticClassifier:
        key = self.groups.get(relative, frozenset([relative]))
        if key not in self.models:
            kept = [e for e in self.examples if not key & set(e.group.split("+"))]
            self.models[key] = train(kept)
        return self.models[key]


def run_mode(
    mode: str, doc: ParsedDoc, ask_llm: AskLlm, learned: LogisticClassifier
) -> Classification:
    if mode == "rules":
        return classify(doc)
    if mode == "lr":
        return classify(doc, first=learned)
    if mode == "cascade":
        return classify(doc, ask_llm)
    if mode == "lr_cascade":
        return classify(doc, ask_llm, learned)
    if template_label(doc):
        return classify(doc)
    return LlmClassifier(ask_llm).classify(doc)


def evaluate(corpus: Path, ask_llm: AskLlm) -> list[dict[str, Any]]:
    rows = []
    held_out = HeldOut(labelled_examples(corpus, ("brownfield", "hard")), source_groups(corpus))
    for relative, (group, truth) in truth_labels(corpus).items():
        doc, _ = redact_doc(parse_file(corpus / relative))
        learned = held_out.model_for(relative)
        for mode in MODES:
            started = time.perf_counter()
            result = run_mode(mode, doc, ask_llm, learned)
            rows.append(
                {
                    "file": relative,
                    "group": group,
                    "mode": mode,
                    "truth": truth,
                    "label": result.label,
                    "confidence": round(result.confidence, 3),
                    "method": result.method,
                    "needs_review": needs_review(result),
                    "correct": result.label == truth,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
    return rows


def summary(rows: list[dict[str, Any]]) -> list[str]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["group"], row["mode"])].append(row)
    lines = [
        f"{'group':<11} {'mode':<10} {'docs':>4} {'correct':>8} {'review':>7} "
        f"{'confident wrong':>16} {'llm used':>9}"
    ]
    for (group, mode), items in sorted(groups.items()):
        correct = sum(r["correct"] for r in items)
        review = sum(r["needs_review"] for r in items)
        wrong = sum(not r["correct"] and not r["needs_review"] for r in items)
        llm = sum(r["method"] == "llm" for r in items)
        lines.append(
            f"{group:<11} {mode:<10} {len(items):>4} {correct:>8} {review:>7} {wrong:>16} {llm:>9}"
        )
    return lines


def main() -> None:
    settings = Settings()
    llm = build_llm(settings, PostgresLedger(settings.database_url).record)
    rows = evaluate(CORPUS, label_asker(llm))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("".join(json.dumps(row) + "\n" for row in rows))
    print("\n".join(summary(rows)))
    print(f"raw rows={len(rows)} file={OUTPUT}")


if __name__ == "__main__":
    main()
