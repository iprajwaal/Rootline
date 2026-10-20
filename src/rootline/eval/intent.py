import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from rootline.agent.guard import guard
from rootline.agent.understand import (
    SURE,
    AskIntent,
    Guess,
    Understander,
    Understanding,
    intent_asker,
    intent_prompt,
    read_signals,
    understanding,
)
from rootline.catalog import Catalog, load_catalog
from rootline.config import Settings
from rootline.llm.ledger import PostgresLedger
from rootline.llm.structured import build_llm

QUERIES = Path("eval/queries/intent_dev.jsonl")
OUTPUT = Path("eval/runs/intent_dev.jsonl")
MODES = ("rules", "cascade", "llm")


def run_mode(mode: str, text: str, catalog: Catalog, ask_intent: AskIntent) -> Understanding:
    if mode != "llm":
        return Understander(catalog, ask_intent if mode == "cascade" else None).understand(text)
    # The baseline: the model's answer as it is, with no rules and no agreement check.
    verdict = ask_intent(intent_prompt(text))
    intent = verdict.intent if verdict.confidence >= SURE else "unclear"
    guess = Guess(intent, verdict.confidence, verdict.reason)
    return understanding(guess, read_signals(text, catalog), "llm")


def evaluate(queries: Path, catalog: Catalog, ask_intent: AskIntent) -> list[dict[str, Any]]:
    rows = []
    for line in queries.read_text().splitlines():
        query = json.loads(line)
        text = guard(query["text"]).text
        for mode in MODES:
            started = time.perf_counter()
            found = run_mode(mode, text, catalog, ask_intent)
            correct = (found.intent, found.lane) == (query["intent"], query["lane"])
            rows.append(
                {
                    "text": query["text"],
                    "kind": query["kind"],
                    "mode": mode,
                    "truth": query["intent"],
                    "truth_lane": query["lane"],
                    "intent": found.intent,
                    "lane": found.lane,
                    "confidence": round(found.confidence, 3),
                    "method": found.method,
                    "correct": correct,
                    # Asking back is a safe miss; acting on the wrong intent is not.
                    "confident_wrong": not correct and found.intent != "unclear",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
    return rows


def summary(rows: list[dict[str, Any]]) -> list[str]:
    modes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        modes[row["mode"]].append(row)
    lines = [
        f"{'mode':<8} {'queries':>7} {'correct':>8} {'asked back':>11}"
        f" {'confident wrong':>16} {'llm used':>9}"
    ]
    for mode in MODES:
        items = modes[mode]
        correct = sum(r["correct"] for r in items)
        wrong = sum(r["confident_wrong"] for r in items)
        asked = len(items) - correct - wrong
        llm = sum(r["method"] == "llm" for r in items)
        lines.append(f"{mode:<8} {len(items):>7} {correct:>8} {asked:>11} {wrong:>16} {llm:>9}")
    kinds: dict[str, list[bool]] = defaultdict(list)
    for row in modes["cascade"]:
        kinds[row["kind"]].append(row["correct"])
    lines.append(
        "cascade by kind: " + ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in kinds.items())
    )
    return lines


def main() -> None:
    settings = Settings()
    llm = build_llm(settings, PostgresLedger(settings.database_url).record)
    rows = evaluate(QUERIES, load_catalog(settings.catalog_path), intent_asker(llm))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("".join(json.dumps(row) + "\n" for row in rows))
    print("\n".join(summary(rows)))
    for row in rows:
        if row["mode"] == "cascade" and not row["correct"]:
            print(
                f"wrong: {row['text'][:70]!r} want {row['truth']} got {row['intent']} {row['lane']}"
            )
    print(f"raw rows={len(rows)} file={OUTPUT}")


if __name__ == "__main__":
    main()
