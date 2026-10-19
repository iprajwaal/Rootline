import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from rootline.config import Settings
from rootline.ingest.classify import (
    AskLlm,
    classify,
    llm_prompt,
    needs_review,
    template_label,
)
from rootline.ingest.factory import label_asker
from rootline.ingest.parsing import parse_file
from rootline.ingest.redaction import redact_doc
from rootline.llm.ledger import PostgresLedger
from rootline.llm.structured import build_llm
from rootline.models import Classification, ParsedDoc

CORPUS = Path("corpus")
OUTPUT = Path("eval/runs/classification.jsonl")
MODES = ("rules", "cascade", "llm")


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


def run_mode(mode: str, doc: ParsedDoc, ask_llm: AskLlm) -> Classification:
    if mode == "rules":
        return classify(doc)
    if mode == "cascade" or template_label(doc):
        return classify(doc, ask_llm)
    verdict = ask_llm(llm_prompt(doc))
    return Classification(
        label=verdict.label, confidence=verdict.confidence, method="llm", evidence=[verdict.reason]
    )


def evaluate(corpus: Path, ask_llm: AskLlm) -> list[dict[str, Any]]:
    rows = []
    for relative, (group, truth) in truth_labels(corpus).items():
        doc, _ = redact_doc(parse_file(corpus / relative))
        for mode in MODES:
            started = time.perf_counter()
            result = run_mode(mode, doc, ask_llm)
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
        f"{'group':<11} {'mode':<8} {'docs':>4} {'correct':>8} {'review':>7} "
        f"{'confident wrong':>16} {'llm used':>9}"
    ]
    for (group, mode), items in sorted(groups.items()):
        correct = sum(r["correct"] for r in items)
        review = sum(r["needs_review"] for r in items)
        wrong = sum(not r["correct"] and not r["needs_review"] for r in items)
        llm = sum(r["method"] == "llm" for r in items)
        lines.append(
            f"{group:<11} {mode:<8} {len(items):>4} {correct:>8} {review:>7} {wrong:>16} {llm:>9}"
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
