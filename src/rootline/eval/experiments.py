import json
from collections import defaultdict
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

import psycopg

from rootline.agent.guard import guard
from rootline.agent.understand import SURE, intent_asker
from rootline.config import Settings
from rootline.eval import classification
from rootline.eval.intent import run_mode
from rootline.eval.runner import (
    LANES,
    NOISY_KINDS,
    QUERIES,
    RUNS,
    Row,
    interval,
    mean_ci,
    noisy_text,
    prepared,
    ratio,
    scored,
)
from rootline.ingest.analysis import Analyzer
from rootline.ingest.factory import build_analyzer, label_asker
from rootline.llm.ledger import PostgresLedger
from rootline.llm.structured import build_llm
from rootline.retrieval.search import Searcher
from rootline.retrieval.suggest import Suggester

METHODS = {
    "keyword": frozenset({"keyword"}),
    "vector": frozenset({"vector"}),
    "hybrid": frozenset({"keyword", "vector"}),
}
# Meaning alone, and meaning backed by two matching words: the two acceptance floors.
ALONE = (0.62, 0.64, 0.66, 0.68, 0.70)
WITH_EVIDENCE = (0.54, 0.56, 0.58, 0.60)


def main() -> None:
    # Everything here reads dev only; the held-out test set is never opened.
    queries = [json.loads(line) for line in (QUERIES / "dev.jsonl").open()]
    settings = prepared(Settings())
    analyzer = build_analyzer(settings)
    out = RUNS / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-experiments"
    out.mkdir(parents=True)
    with psycopg.connect(settings.database_url) as conn:
        modes = {
            name: retrieval_rows(queries, conn, analyzer, methods)
            for name, methods in METHODS.items()
        }
        sweep = {
            (alone, backed): retrieval_rows(
                queries, conn, analyzer, METHODS["hybrid"], (alone, backed)
            )
            for alone, backed in product(ALONE, WITH_EVIDENCE)
        }
    intents = intent_rows(queries, settings, analyzer)
    classified = classification.evaluate(
        Path("corpus"),
        label_asker(build_llm(settings, PostgresLedger(settings.database_url).record)),
    )
    raw = {"search_modes": modes, "intent": intents, "classification": classified}
    (out / "raw.json").write_text(json.dumps(raw, default=str) + "\n")
    sweep_rows = [
        {"alone": a, "with_evidence": b, **quality(rows)} for (a, b), rows in sweep.items()
    ]
    (out / "sweep.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sweep_rows))
    (out / "report.md").write_text(report(modes, sweep_rows, intents, classified))
    print(f"wrote {out}/report.md")


def retrieval_rows(
    queries: list[Row],
    conn: psycopg.Connection[Any],
    analyzer: Analyzer,
    methods: frozenset[str],
    floors: tuple[float, float] | None = None,
) -> list[Row]:
    searcher = Searcher(conn, analyzer.embedder, analyzer.catalog.services, methods)
    if floors:
        searcher = Searcher(conn, analyzer.embedder, analyzer.catalog.services, methods, floors)
    suggester = Suggester(searcher, analyzer.catalog.alerts)
    rows = []
    for query in queries:
        if query["lane"] is None:
            continue
        clean = lists(searcher, suggester, query, query["text"])
        noisy = None
        if query["kind"] in NOISY_KINDS:
            noisy = lists(searcher, suggester, query, noisy_text(query["text"]))
        for lane in LANES if query["lane"] == "both" else (query["lane"],):
            rows.append(scored(query, lane, clean, noisy))
    return rows


def lists(searcher: Searcher, suggester: Suggester, query: Row, text: str) -> dict[str, Any]:
    # The same calls the API's /suggest and /search make, without the HTTP layer.
    if query["intent"] == "suggest":
        body = suggester.suggest(text).model_dump()
        return {"found": {lane: body[lane]["suggestions"] for lane in LANES}, "ms": 0.0}
    body = searcher.search(text).model_dump()
    return {"found": {lane: body[lane]["hits"] for lane in LANES}, "ms": 0.0}


def quality(rows: list[Row]) -> dict[str, Any]:
    answerable = [r for r in rows if r["answerable"]]
    abstained = [r for r in rows if r["abstained"]]
    right = [r for r in abstained if not r["answerable"]]
    should = [r for r in rows if not r["answerable"]]
    precision = ratio(len(right), len(abstained)) or 0.0
    recall = ratio(len(right), len(should)) or 0.0
    f1 = round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0.0
    exact = mean_ci(r["hit1"] for r in answerable)
    return {
        "exact@1": exact,
        "mrr": mean_ci(r["rr"] for r in answerable),
        "recall@5": mean_ci(r["recall5"] for r in answerable),
        "abstain_precision": precision,
        "abstain_recall": recall,
        "abstain_f1": f1,
        "wrong_type": mean_ci(r["wrong_type"] for r in rows),
        # One number to pick a setting: finding the right document and staying quiet when
        # there is none count the same.
        "score": round(((exact["mean"] or 0.0) + f1) / 2, 3),
    }


def intent_rows(queries: list[Row], settings: Settings, analyzer: Analyzer) -> list[Row]:
    llm = build_llm(settings, PostgresLedger(settings.database_url).record)
    ask = intent_asker(llm)
    rows = []
    for query in queries:
        text = guard(query["text"]).text
        for mode in ("rules", "llm", "cascade"):
            found = run_mode(mode, text, analyzer.catalog, ask)
            rows.append(
                {
                    "id": query["id"],
                    "origin": query["origin"],
                    "mode": mode,
                    "correct": found.intent == query["intent"],
                    "asked_back": found.intent == "unclear" and query["intent"] != "unclear",
                    "confident_wrong": found.intent not in (query["intent"], "unclear"),
                    "llm": found.method == "llm",
                }
            )
    return rows


def report(
    modes: dict[str, list[Row]],
    sweep: list[Row],
    intents: list[Row],
    classified: list[dict[str, Any]],
) -> str:
    lines = [
        "# Boundary experiments and calibration (dev only)",
        "",
        "Every number comes from `eval/queries/dev.jsonl` or the labelled corpus. The held-out"
        " test set is not read here. Brackets are 95% bootstrap intervals.",
        "",
        "## Search: keyword vs vector vs hybrid",
        "",
        "| Method | origin | exact@1 | MRR | recall@5 | abstain P | abstain R | wrong type |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for name, rows in modes.items():
        for origin in ("greenfield", "brownfield", "all"):
            group = [r for r in rows if origin == "all" or r["origin"] in (origin, "none")]
            q = quality(group)
            lines.append(
                f"| {name} | {origin} | {interval(q['exact@1'])} | {interval(q['mrr'])}"
                f" | {interval(q['recall@5'])} | {q['abstain_precision']}"
                f" | {q['abstain_recall']} | {interval(q['wrong_type'])} |"
            )
    best = max(sweep, key=lambda r: r["score"])
    lines += [
        "",
        "## Threshold sweep (hybrid)",
        "",
        "`alone`: similarity a document needs on meaning alone. `with evidence`: what it needs"
        " when two words or a named service also match. Score = mean of exact@1 and abstain"
        f" F1. Best: alone {best['alone']}, with evidence {best['with_evidence']}"
        f" (score {best['score']}).",
        "",
        "| alone | with evidence | exact@1 | MRR | abstain P | abstain R | abstain F1 | score |",
        "| ---: | ---: | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for r in sorted(sweep, key=lambda r: (r["alone"], r["with_evidence"])):
        lines.append(
            f"| {r['alone']} | {r['with_evidence']} | {interval(r['exact@1'])}"
            f" | {interval(r['mrr'])} | {r['abstain_precision']} | {r['abstain_recall']}"
            f" | {r['abstain_f1']} | {r['score']} |"
        )
    lines += [
        "",
        f"## Intent: rules vs LLM vs cascade (the LLM decides below {SURE})",
        "",
        "| Mode | n | correct | asked back | confident wrong | LLM used |",
        "| --- | ---: | --- | ---: | --- | ---: |",
    ]
    for mode in ("rules", "llm", "cascade"):
        group = [r for r in intents if r["mode"] == mode]
        lines.append(
            f"| {mode} | {len(group)} | {interval(mean_ci(r['correct'] for r in group))}"
            f" | {sum(r['asked_back'] for r in group)}"
            f" | {interval(mean_ci(r['confident_wrong'] for r in group))}"
            f" | {sum(r['llm'] for r in group)} |"
        )
    lines += [
        "",
        "## Classification: rules vs logistic regression vs LLM vs cascade",
        "",
        "Over every labelled corpus file. A wrong label sent to review is a safe miss; a wrong"
        " label stored without review is a confident wrong.",
        "",
        "| Origin | mode | docs | correct | to review | confident wrong | LLM used |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in classified:
        groups[(row["group"], row["mode"])].append(row)
    for (origin, mode), items in sorted(groups.items()):
        wrong = sum(not r["correct"] and not r["needs_review"] for r in items)
        lines.append(
            f"| {origin} | {mode} | {len(items)} | {sum(r['correct'] for r in items)}"
            f" | {sum(r['needs_review'] for r in items)} | {wrong}"
            f" | {sum(r['method'] == 'llm' for r in items)} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
