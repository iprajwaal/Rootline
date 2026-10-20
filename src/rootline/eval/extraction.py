import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from rootline.catalog import load_catalog
from rootline.config import Settings
from rootline.core.models import ParsedDoc, SopRecord
from rootline.corpus.layout import load_document
from rootline.ingest.classify import classify, split_mixed, template_label
from rootline.ingest.extract import Record, extract
from rootline.ingest.factory import label_asker, record_asker
from rootline.ingest.facts import extract_facts
from rootline.ingest.parsing import parse_file
from rootline.ingest.redaction import redact_doc
from rootline.llm.ledger import PostgresLedger
from rootline.llm.structured import StructuredLLM, build_llm

CORPUS = Path("corpus")
OUTPUT = Path("eval/runs/extraction.jsonl")


def source_records(corpus: Path) -> dict[str, Record]:
    records = {}
    for folder in ("greenfield", "brownfield"):
        for path in sorted((corpus / "sources" / folder).glob("*.yaml")):
            meta, record = load_document(path)
            records[meta.doc_id] = record
    return records


def evaluate(corpus: Path, llm: StructuredLLM) -> list[dict[str, Any]]:
    catalog = load_catalog(corpus / "catalog.yaml")
    sources = source_records(corpus)

    ask_label = label_asker(llm)
    ask_record = record_asker(llm)

    rows = []
    truth = (corpus / "ground_truth" / "brownfield.jsonl").read_text().splitlines()
    for line in truth:
        row = json.loads(line)
        doc, _ = redact_doc(parse_file(corpus / row["file"]))
        facts = extract_facts(doc, catalog)
        label = classify(doc, ask_label).label
        parts: list[tuple[ParsedDoc, str]] = (
            list(zip(split_mixed(doc), ("rca", "sop"), strict=True))
            if label == "mixed"
            else [(doc, label)]
        )
        for part, part_label in parts:
            if part_label not in ("rca", "sop"):
                continue
            result = extract(part, part_label, template_label(part) is not None, ask_record)  # type: ignore[arg-type]
            expected = next(
                (sources[i] for i in row["source_ids"] if i.lower().startswith(part_label)), None
            )
            rows.append(
                {
                    "file": row["file"],
                    "format": row["format"],
                    "part": part_label,
                    "method": result.method,
                    "verified": result.verified,
                    "issues": result.issues,
                    "steps": step_count(result.record),
                    "expected_steps": step_count(expected),
                    "services": facts.services,
                    "expected_services": row["services"],
                    "effective_date": facts.effective_date,
                }
            )
    return rows


def step_count(record: Record | None) -> int | None:
    return len(record.steps) if isinstance(record, SopRecord) else None


def summary(rows: list[dict[str, Any]]) -> list[str]:
    by_format: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_format[row["format"]].append(row)
    lines = [f"{'format':<7} {'parts':>5} {'verified':>9} {'sop steps exact':>16} {'dated':>6}"]
    for name, items in sorted(by_format.items()):
        sops = [r for r in items if r["part"] == "sop"]
        exact = sum(r["steps"] == r["expected_steps"] for r in sops)
        lines.append(
            f"{name:<7} {len(items):>5} {sum(r['verified'] for r in items):>9} "
            f"{f'{exact}/{len(sops)}':>16} {sum(bool(r['effective_date']) for r in items):>6}"
        )
    tp = sum(len(set(r["services"]) & set(r["expected_services"])) for r in rows)
    found = sum(len(r["services"]) for r in rows)
    wanted = sum(len(r["expected_services"]) for r in rows)
    lines.append(f"services precision {tp / found:.2f} recall {tp / wanted:.2f}")
    return lines


def main() -> None:
    settings = Settings()
    llm = build_llm(settings, PostgresLedger(settings.database_url).record)
    rows = evaluate(CORPUS, llm)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("".join(json.dumps(row) + "\n" for row in rows))
    print("\n".join(summary(rows)))
    for row in rows:
        if not row["verified"]:
            print(f"unverified: {row['file']} [{row['part']}] {row['issues'][:2]}")
    print(f"raw rows={len(rows)} file={OUTPUT}")


if __name__ == "__main__":
    main()
