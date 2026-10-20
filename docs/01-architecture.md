# Architecture

## Services

`docker compose up` (or `make demo`, which adds ShopLite):

| Service | Port | |
|---|---|---|
| postgres | 5432 | pgvector/pgvector:pg16 |
| migrate | | applies `db/migrations/*.sql`, then exits |
| seed | | `rootline ingest` on the corpus, then exits. Re-runs are no-ops |
| api | 8000 | FastAPI |
| agent | 2024 | Aegra serving the LangGraph graph; threads and checkpoints in Postgres |
| ui | 3000 | agent-chat-ui plus [`ui/patches/alerts.patch`](../ui/patches/alerts.patch) (alerts panel) |
| shoplite | 8080 | `demo` profile only |

The kind cluster runs outside compose (`make cluster`). The agent talks to it through
`cluster/.kube/config`, which is mounted read-only.

We use Aegra because the official LangGraph server wants a license key to start. The graph itself
is plain LangGraph, so switching back is a config change.

## Code layout

```
core/        models, text cleanup, request context
ingest/      parsers, redaction, classify, extract, facts, chunking, embedding, duplicates, store
retrieval/   search, suggest, issue (log cleanup), passages, documents, references
llm/         StructuredLLM (cached calls), ledger, tracing
agent/       graph, understand, guard, answer, troubleshoot, toolbox, executor, kube, drafts, present
api/         FastAPI app and Jinja pages
eval/        split, runner, experiments
incidents.py review.py ops.py scenarios.py catalog.py config.py cli.py
```

Search, suggest and SOP steps work without [`llm/`](../src/rootline/llm). The REST API works without [`agent/`](../src/rootline/agent).

`Sandbox` in [`agent/executor.py`](../src/rootline/agent/executor.py) is the only Protocol. It has two implementations: `ShopLite` and
`KubeShop` ([`agent/kube.py`](../src/rootline/agent/kube.py)).

## Tables

Everything is in one database, so a document, its versions, chunks and steps change in one
transaction.

- `documents`: one row per doc. Type (rca/sop/ref), origin (greenfield/brownfield/reference),
  status (active/superseded/deleted), services, alerts, cluster, confidence, `source_key`
  (unique), current version.
- `document_versions`: every version, with raw hash, content hash, parsed blocks, extracted
  record and facts.
- `aliases`: other file names for the same bytes or content.
- `chunks`: partitioned into `chunks_rca`, `chunks_sop` and `chunks_ref`, each with its own HNSW
  and GIN index. Embeddings are `vector(768)`.
- `sop_steps`: ordinal, instruction, command, catalog action/check, expected result,
  section (steps/rollback).
- `doc_links`: related, supersedes, split_from.
- `review_items`: unknown_type, low_type_confidence, extraction_unverified, secrets_redacted,
  possible_duplicate, draft.
- `incidents`: alerts received, keyed by fingerprint.
- `ledger`: one row per LLM/embedding call (tokens, cost, latency, degraded).
- `requests`: one row per chat turn.
- `audit`: ingests, deletes, approvals.

Raw uploads are stored by hash in the `raw` volume.

## Models

Gemini Flash-Lite (`LLM_SMALL_MODEL`) for everything that needs an LLM: unsure intents, unsure
classifications, brownfield extraction, RCA answers, the agent, and drafts. Vertex
`text-embedding-005` for embeddings.

All structured calls go through `StructuredLLM.ask`. Answers are cached in
`cache/llm/<task>/<sha>.json`, keyed on task, model and prompt; embeddings in [`cache/embeddings/`](../cache/embeddings).
Both caches are committed, which keeps tests and eval offline.
