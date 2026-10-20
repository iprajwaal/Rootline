# Eval

## Queries

[`eval/queries/queries.jsonl`](../eval/queries/queries.jsonl): 118 labelled requests.

```json
{"id": "q038", "text": "[FIRING:1] OrdersDbPoolExhausted orders-api critical", "kind": "alert",
 "intent": "suggest", "lane": "both", "origin": "greenfield",
 "rca": ["RCA-2025-014"], "sop": ["SOP-DB-001"]}
```

- Kinds: `id`, `title`, `service`, `error`, `description`, `alert`, `stack_trace`, `chat_rca`,
  `chat_sop`, `ambiguous`, plus no-answer queries (empty expected lists).
- [`eval/split.py`](../src/rootline/eval/split.py) splits within each kind, seed 7, 70/30: 84 dev and 34 test. The test file hash
  is saved in `test.sha256`.
- Tuning uses dev only. The test split is meant to run once.

## Runner

```bash
make eval SPLIT=dev     # or SPLIT=test
```

[`eval/runner.py`](../src/rootline/eval/runner.py) builds a fresh `rootline_eval` database from the corpus, then runs three passes:

- **retrieval**, through `/search` or `/suggest`: exact@1, MRR, recall@5 per list and origin,
  wrong-type rate, abstain precision/recall. Errors, alerts and traces also run with log noise
  added.
- **intent**: accuracy and confident-wrong rate per origin, plus the list of misses.
- **turns**, through the full graph: where each turn ended, SOP steps word for word, RCA
  citations valid and pointing at the right incident, plus latency, LLM calls, cache share,
  tokens and cost per route.

Output goes to `eval/runs/<time>-<split>/`: `retrieval.jsonl`, `intent.jsonl`, `graph.jsonl`,
`metrics.json`, `report.md`. Rates come with 95% bootstrap intervals (1,000 resamples).

LLM answers come from the committed cache, so the report's cost column is mostly 0. Live costs
come from the `ledger`.

## Experiments

`make experiments` (dev only) compares:
- keyword vs vector vs hybrid search;
- a threshold sweep (alone 0.62 to 0.70, with evidence 0.54 to 0.60);
- intent: rules vs LLM vs cascade;
- classification: rules vs LR vs LLM vs cascades, over every labelled corpus file.

Output: `eval/runs/<time>-experiments/`.

## Scenarios

`make scenarios` runs against a running `make demo`. Each `scenarios/*.yaml` sets a fault, an
entry point (alert or chat), the turns, required and forbidden text, LLM-call and time budgets, the
review decision, and the expected end health.

| | |
|---|---|
| s1 | DB pool alert: SOP and RCA directly, 0 LLM calls |
| s1b | s1 with no key and empty caches |
| s2 | "kafka lag??": two similar SOPs side by side |
| s3 | disk full, no docs: agent, dry run, write-up |
| s4 | disk full again: the SOP from s3, no LLM, under 1 s |
| s5 | expired payment key: brownfield SOP, key never shown |
| s6 | Redis down, seen as latency: leads to the Redis docs |
| s7 | bad deploy: current SOP first, old one superseded |
| s8 | worker OOM pasted log: split RCA + SOP |
| s9 | DB read-only alert, no docs: agent, yes to write-up, approve |

Runs are saved in [`scenarios/runs/`](../scenarios/runs).

## Tests

`make check` runs ruff, mypy, and 809 tests (798 + 11 in ShopLite), all offline. The ones tied to
requirements:

- [`test_ingest_idempotency.py`](../tests/test_ingest_idempotency.py): ingest twice, 8 parallel processes, edit, delete
- [`test_hardening.py`](../tests/test_hardening.py): list isolation, partition isolation, canary scans (rows, API, logs, LLM
  cache), odd input, garbage uploads, no-LLM mode
- [`test_suggest.py`](../tests/test_suggest.py): each fault's RCA and SOP, noise, unseen errors
- [`test_agent_graph.py`](../tests/test_agent_graph.py): RCA citations, approvals, routing
- [`test_learning.py`](../tests/test_learning.py): drafts, review, versions

If you change test text that needs new embeddings, run once with `ROOTLINE_TEST_EMBED_KEY` set to
fill the cache.
