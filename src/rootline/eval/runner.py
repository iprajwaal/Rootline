import argparse
import json
import random
import re
import statistics
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from psycopg.conninfo import make_conninfo

from rootline.agent.factory import default_graph
from rootline.agent.guard import guard
from rootline.agent.present import escape
from rootline.agent.understand import Understander, intent_asker
from rootline.api.app import app, get_analyzer, get_settings
from rootline.config import Settings
from rootline.db.migrate import migrate
from rootline.ingest.analysis import Analyzer
from rootline.ingest.factory import build_analyzer, ingest_path
from rootline.ingest.store import Store
from rootline.llm.ledger import PostgresLedger
from rootline.llm.structured import build_llm

QUERIES = Path("eval/queries")
RUNS = Path("eval/runs")
CORPUS = ("greenfield", "brownfield", "reference", "hard")
EVAL_DB = "rootline_eval"
LANES = ("rca", "sop")
# Pasted errors and alerts also run wrapped in log noise, to measure what the noise costs.
NOISY_KINDS = ("error", "alert", "stack_trace")
RESAMPLES = 1000
SEED = 11
SOP_HEADER = re.compile(r"\*\*\\\[SOP\\\] (SOP-[\w-]+) · v(\d+)\*\*")
CITED = re.compile(r"\[\\\[(\d+)\\\]\]\([^)]*/docs/([\w-]+)#[\w-]*\)")
SOURCE = re.compile(r"^(\d+)\. \[\\\[RCA\\\] ([\w-]+)\]", re.MULTILINE)
STEPS = """
    select instruction, command from sop_steps where doc_id = %s and version = %s
    order by section = 'rollback', ordinal
"""
TURN = """
    select r.ms, r.llm_calls, coalesce(sum(l.input_tokens), 0), coalesce(sum(l.output_tokens), 0),
        coalesce(sum(l.cost_usd), 0), count(*) filter (where l.kind = 'llm_cache'), r.path
    from requests r left join ledger l using (request_id)
    where r.request_id = %s group by r.ms, r.llm_calls, r.path
"""

Row = dict[str, Any]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the eval queries and write a report.")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    args = parser.parse_args()
    queries = [json.loads(line) for line in (QUERIES / f"{args.split}.jsonl").open()]
    settings = prepared(Settings())
    analyzer = build_analyzer(settings)
    out = RUNS / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{args.split}"
    out.mkdir(parents=True)
    rows = {
        "retrieval": retrieval(queries, settings, analyzer),
        "intent": intents(queries, settings, analyzer),
        "graph": turns(queries, settings),
    }
    for name, found in rows.items():
        (out / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in found))
    metrics = summarize(rows)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (out / "report.md").write_text(report(metrics, args.split, len(queries), settings))
    print(f"wrote {out}/report.md")


def prepared(settings: Settings) -> Settings:
    # A fresh database holding exactly the corpus, so every run measures the same library.
    with psycopg.connect(settings.database_url, autocommit=True) as admin:
        admin.execute(f"drop database if exists {EVAL_DB} with (force)")
        admin.execute(f"create database {EVAL_DB}")
    url = make_conninfo(settings.database_url, dbname=EVAL_DB)
    evaluated = settings.model_copy(update={"database_url": url})
    analyzer = build_analyzer(evaluated)
    with psycopg.connect(url, autocommit=True) as conn, TemporaryDirectory() as raw:
        migrate(conn)
        store = Store(conn, analyzer, Path(raw), evaluated.embed_model, "eval")
        for folder in CORPUS:
            ingest_path(store, evaluated.corpus_dir / folder)
    return evaluated


def retrieval(queries: list[Row], settings: Settings, analyzer: Analyzer) -> list[Row]:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    client = TestClient(app)
    rows = []
    for query in queries:
        if query["lane"] is None:
            continue
        clean = lists(client, query, query["text"])
        noisy = (
            lists(client, query, noisy_text(query["text"]))
            if query["kind"] in NOISY_KINDS
            else None
        )
        for lane in LANES if query["lane"] == "both" else (query["lane"],):
            rows.append(scored(query, lane, clean, noisy))
    app.dependency_overrides.clear()
    return rows


def lists(client: TestClient, query: Row, text: str) -> dict[str, Any]:
    # Alerts and errors go where the alert path sends them; everything else is a lookup.
    started = time.perf_counter()
    if query["intent"] == "suggest":
        body = client.post("/suggest", json={"text": text}).json()
        found = {lane: body[lane]["suggestions"] for lane in LANES}
    else:
        body = client.get("/search", params={"q": text}).json()
        found = {lane: body[lane]["hits"] for lane in LANES}
    return {"found": found, "ms": (time.perf_counter() - started) * 1000}


def scored(query: Row, lane: str, clean: dict[str, Any], noisy: dict[str, Any] | None) -> Row:
    expected = query[lane]
    hits = clean["found"][lane]
    ids = [hit["doc_id"] for hit in hits]
    rank = next((n for n, doc_id in enumerate(ids, start=1) if doc_id in expected), None)
    noisy_ids = [hit["doc_id"] for hit in noisy["found"][lane]] if noisy else None
    return {
        "id": query["id"],
        "kind": query["kind"],
        "origin": query["origin"],
        "lane": lane,
        "expected": expected,
        "got": ids,
        "answerable": bool(expected),
        "hit1": rank == 1,
        "rr": 1 / rank if rank else 0.0,
        "recall5": rank is not None and rank <= 5,
        "wrong_type": any(hit["doc_type"] != lane for hit in hits),
        "abstained": not ids,
        "noisy_hit1": bool(noisy_ids) and noisy_ids[0] in expected
        if noisy_ids is not None
        else None,
        "ms": round(clean["ms"], 1),
    }


def intents(queries: list[Row], settings: Settings, analyzer: Analyzer) -> list[Row]:
    llm = build_llm(settings, PostgresLedger(settings.database_url).record)
    understander = Understander(analyzer.catalog, intent_asker(llm))
    rows = []
    for query in queries:
        found = understander.understand(guard(query["text"]).text)
        rows.append(
            {
                "id": query["id"],
                "kind": query["kind"],
                "origin": query["origin"],
                "truth": query["intent"],
                "intent": found.intent,
                "method": found.method,
                "correct": found.intent == query["intent"],
                # Asking back is a safe miss; acting on the wrong intent is not.
                "confident_wrong": found.intent not in (query["intent"], "unclear"),
            }
        )
    return rows


def turns(queries: list[Row], settings: Settings) -> list[Row]:
    graph = default_graph(settings)
    rows = []
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        for query in queries:
            state = graph.invoke({"messages": [HumanMessage(content=query["text"])]})
            reply = state["messages"][-1].text
            measured = conn.execute(TURN, (state["request_id"],)).fetchone()
            ms, llm_calls, input_tokens, output_tokens, cost, cached, path = measured or (
                *(0,) * 6,
                "",
            )
            row = {
                "id": query["id"],
                "kind": query["kind"],
                "origin": query["origin"],
                "truth": query["intent"],
                "route": state["understanding"].intent,
                "ms": ms,
                "llm_calls": llm_calls,
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "cost": float(cost),
                "cached_calls": cached,
                # The last step before the turn was recorded: what the user actually got.
                "ended": path.split(" → ")[-1],
            }
            if query["kind"] == "chat_sop" and query["sop"]:
                row["step_exact"] = steps_match(conn, reply)
            if query["kind"] == "chat_rca":
                row |= citations(reply, query["rca"])
            rows.append(row)
    return rows


def steps_match(conn: psycopg.Connection[Any], reply: str) -> bool | None:
    # Every stored instruction and command appears in the reply, in the stored order. None
    # when no procedure was shown, such as when two SOPs tie and the user is asked to choose.
    header = SOP_HEADER.search(reply)
    if header is None:
        return None
    stored = conn.execute(STEPS, (header.group(1), int(header.group(2)))).fetchall()
    position = 0
    for instruction, command in stored:
        for part in (escape(instruction), f"`{command}`" if command else None):
            if part is None:
                continue
            found = reply.find(part, position)
            if found < 0:
                return False
            position = found
    return bool(stored)


def citations(reply: str, expected: list[str]) -> Row:
    cited = CITED.findall(reply)
    sources = dict(SOURCE.findall(reply))
    return {
        "answered": bool(cited),
        "citations_valid": bool(cited) and all(sources.get(n) == doc for n, doc in cited),
        "cites_expected": any(doc in expected for _, doc in cited),
    }


def noisy_text(text: str) -> str:
    return "\n".join(
        [
            "\x1b[2m2026-10-20T03:14:06.002Z\x1b[0m INFO [orders-api-7d9f8b6c5-x2klm]"
            " GET /healthz 200",
            "2026-10-20T03:14:06.115Z DEBUG [orders-api-7d9f8b6c5-x2klm] cache hit ratio 0.93",
            text,
            "2026-10-20T03:14:07.004Z INFO [orders-api-7d9f8b6c5-x2klm] GET /metrics 200 in 2ms",
        ]
    )


def summarize(rows: dict[str, list[Row]]) -> dict[str, Any]:
    found = rows["retrieval"]
    answerable = [r for r in found if r["answerable"]]
    metrics: dict[str, Any] = {"retrieval": {}, "by_kind": {}}
    for lane in LANES:
        for origin in ("greenfield", "brownfield", "all"):
            group = [r for r in answerable if r["lane"] == lane and origin in ("all", r["origin"])]
            if group:
                metrics["retrieval"][f"{lane}/{origin}"] = {
                    "n": len(group),
                    "exact@1": mean_ci(r["hit1"] for r in group),
                    "mrr": mean_ci(r["rr"] for r in group),
                    "recall@5": mean_ci(r["recall5"] for r in group),
                }
    for kind in sorted({r["kind"] for r in answerable}):
        group = [r for r in answerable if r["kind"] == kind]
        metrics["by_kind"][kind] = {
            "n": len(group),
            "exact@1": mean_ci(r["hit1"] for r in group),
            "mrr": mean_ci(r["rr"] for r in group),
        }
    metrics["wrong_type_rate"] = mean_ci(r["wrong_type"] for r in found)
    should = [r for r in found if not r["answerable"]]
    abstained = [r for r in found if r["abstained"]]
    right = [r for r in abstained if not r["answerable"]]
    metrics["abstain"] = {
        "precision": ratio(len(right), len(abstained)),
        "recall": ratio(len(right), len(should)),
        "should_abstain": len(should),
        "abstained": len(abstained),
    }
    paired = [r for r in answerable if r["noisy_hit1"] is not None]
    metrics["noise_delta"] = {
        "n": len(paired),
        "clean_exact@1": mean_ci(r["hit1"] for r in paired),
        "noisy_exact@1": mean_ci(r["noisy_hit1"] for r in paired),
        "delta": mean_ci(float(r["hit1"]) - float(r["noisy_hit1"]) for r in paired),
    }
    said = rows["intent"]
    metrics["intent"] = {
        origin: {
            "n": len(group),
            "accuracy": mean_ci(r["correct"] for r in group),
            "confident_wrong": mean_ci(r["confident_wrong"] for r in group),
        }
        for origin in ("greenfield", "brownfield", "none", "all")
        if (group := [r for r in said if origin in ("all", r["origin"])])
    }
    graph = rows["graph"]
    stepped = [r for r in graph if r.get("step_exact") is not None]
    tied = [r for r in graph if "step_exact" in r and r["step_exact"] is None]
    cited = [r for r in graph if r.get("answered")]
    metrics["steps"] = {
        "n": len(stepped),
        "asked_to_choose": len(tied),
        "exact_match": mean_ci(r["step_exact"] for r in stepped),
    }
    confusions: dict[str, int] = defaultdict(int)
    for r in said:
        if not r["correct"]:
            confusions[f"{r['truth']} → {r['intent']}"] += 1
    metrics["intent_confusions"] = dict(sorted(confusions.items(), key=lambda kv: -kv[1]))
    ended: dict[str, int] = defaultdict(int)
    for r in graph:
        ended[r["ended"]] += 1
    metrics["turns_ended"] = dict(sorted(ended.items(), key=lambda kv: -kv[1]))
    metrics["citations"] = {
        "answered": len(cited),
        "asked": len([r for r in graph if "answered" in r]),
        "valid": mean_ci(r["citations_valid"] for r in cited),
        "cites_expected": mean_ci(r["cites_expected"] for r in cited),
    }
    metrics["cost"] = {
        route: {
            "n": len(group),
            "p50_ms": percentile([r["ms"] for r in group], 0.5),
            "p95_ms": percentile([r["ms"] for r in group], 0.95),
            "llm_calls": round(statistics.mean(r["llm_calls"] for r in group), 2),
            "input_tokens": round(statistics.mean(r["input_tokens"] for r in group)),
            "output_tokens": round(statistics.mean(r["output_tokens"] for r in group)),
            "usd_per_request": round(statistics.mean(r["cost"] for r in group), 6),
            # Answers served from the committed cache cost nothing and return fast.
            "from_cache": ratio(
                sum(r["cached_calls"] for r in group), sum(r["llm_calls"] for r in group)
            ),
        }
        for route in sorted({r["route"] for r in graph})
        if (group := [r for r in graph if r["route"] == route])
    }
    return metrics


def mean_ci(values: Iterable[float | bool]) -> dict[str, float | None]:
    # A 95% percentile bootstrap: the queries are resampled, the mean recomputed each time.
    data = [float(v) for v in values]
    if not data:
        return {"mean": None, "low": None, "high": None}
    rng = random.Random(SEED)
    means = sorted(statistics.mean(rng.choices(data, k=len(data))) for _ in range(RESAMPLES))
    return {
        "mean": round(statistics.mean(data), 3),
        "low": round(means[int(0.025 * RESAMPLES)], 3),
        "high": round(means[int(0.975 * RESAMPLES) - 1], 3),
    }


def ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], 1)


def report(metrics: dict[str, Any], split: str, queries: int, settings: Settings) -> str:
    test_hash = (QUERIES / "test.sha256").read_text().split()[0][:16]
    lines = [
        f"# Eval report: {split}",
        "",
        f"{queries} queries from `eval/queries/{split}.jsonl` · test file sha256 {test_hash}…"
        f" · LLM {settings.llm_small_model} · embeddings {settings.embed_model}"
        f" · {datetime.now(UTC):%Y-%m-%d %H:%M} UTC.",
        "Numbers in brackets are 95% bootstrap intervals over the queries. With groups this"
        " small they are wide; treat a difference inside them as noise.",
        "",
        "## Retrieval by list and origin",
        "",
        "| List / origin | n | exact@1 | MRR | recall@5 |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for name, row in metrics["retrieval"].items():
        lines.append(
            f"| {name} | {row['n']} | {interval(row['exact@1'])} | {interval(row['mrr'])}"
            f" | {interval(row['recall@5'])} |"
        )
    lines += ["", "## Retrieval by kind of query", "", "| Kind | n | exact@1 | MRR |",
              "| --- | ---: | --- | --- |"]  # fmt: skip
    for kind, row in metrics["by_kind"].items():
        lines.append(
            f"| {kind} | {row['n']} | {interval(row['exact@1'])} | {interval(row['mrr'])} |"
        )
    abstain, noise = metrics["abstain"], metrics["noise_delta"]
    lines += [
        "",
        "## Safety of the lists",
        "",
        f"- Wrong-type rate (a list showing another type): {interval(metrics['wrong_type_rate'])}",
        f"- Abstain precision {abstain['precision']} · recall {abstain['recall']}"
        f" ({abstain['abstained']} abstained, {abstain['should_abstain']} should have)",
        f"- Log noise: exact@1 {interval(noise['clean_exact@1'])} clean vs"
        f" {interval(noise['noisy_exact@1'])} noisy,"
        f" delta {interval(noise['delta'])} over {noise['n']}",
        "",
        "## Intent",
        "",
        "| Origin | n | accuracy | confident-wrong |",
        "| --- | ---: | --- | --- |",
    ]
    for origin, row in metrics["intent"].items():
        lines.append(
            f"| {origin} | {row['n']} | {interval(row['accuracy'])}"
            f" | {interval(row['confident_wrong'])} |"
        )
    lines += ["", "Misses (expected → got): " + ", ".join(
        f"{pair} ×{count}" for pair, count in metrics["intent_confusions"].items()
    )]  # fmt: skip
    lines += ["", "Where chat turns ended: " + ", ".join(
        f"{node} ×{count}" for node, count in metrics["turns_ended"].items()
    ) + " (`reply` is a question back)."]  # fmt: skip
    steps, cites = metrics["steps"], metrics["citations"]
    lines += [
        "",
        "## Answers",
        "",
        f"- SOP steps word for word: {interval(steps['exact_match'])} over {steps['n']}"
        f" procedures shown; {steps['asked_to_choose']} questions got two equal SOPs"
        " to choose from",
        f"- RCA answers with citations: {cites['answered']} of {cites['asked']};"
        f" citations valid {interval(cites['valid'])}; cite the right incident"
        f" {interval(cites['cites_expected'])}",
        "",
        "## Latency and cost per request kind (full chat turn)",
        "",
        "Calls served from the committed LLM cache cost nothing and return fast, so the share"
        " from cache says how much of a row is the live cost.",
        "",
        "| Route | n | p50 ms | p95 ms | LLM calls | from cache | input tokens | output tokens"
        " | USD |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for route, row in metrics["cost"].items():
        lines.append(
            f"| {route} | {row['n']} | {row['p50_ms']} | {row['p95_ms']} | {row['llm_calls']}"
            f" | {row['from_cache'] if row['from_cache'] is not None else '-'}"
            f" | {row['input_tokens']} | {row['output_tokens']} | {row['usd_per_request']:.6f} |"
        )
    return "\n".join(lines) + "\n"


def interval(value: dict[str, Any]) -> str:
    if value["mean"] is None:
        return "n/a"
    return f"{value['mean']:.2f} [{value['low']:.2f}, {value['high']:.2f}]"


if __name__ == "__main__":
    main()
