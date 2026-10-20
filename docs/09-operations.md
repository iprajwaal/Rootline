# Operations

## Settings

From `.env` (see [`config.py`](../src/rootline/config.py)):

| Variable | Default | |
|---|---|---|
| `GOOGLE_API_KEY` | | LLM and embeddings. Empty = cache only |
| `LLM_VERTEXAI` | false | true for a Vertex key |
| `LLM_SMALL_MODEL` | gemini-3.5-flash-lite | |
| `EMBED_MODEL` | text-embedding-005 | must match the 768-dim column |
| `EMBED_API_KEY` | | falls back to the LLM key |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | | tracing, off unless both keys are set |
| `SHOP_MODE` | shoplite | `k8s` for the kind cluster |
| `STALE_AFTER_DAYS` | 365 | older docs get a "check it still holds" note |
| `PUBLIC_API_URL` | http://localhost:8000 | base URL for links in chat |

`.env` and `cluster/.kube/config` are both gitignored.

## Caches

- `cache/llm/<task>/<sha>.json`: keyed on task, model and the full prompt.
- `cache/embeddings/text-embedding-005/<sha>.txt`: keyed on the text.

Both are committed. Prompt versions (`intent-v2`, `draft-v3`, ...) are part of the prompt, so
changing a prompt simply misses the old entries. To force a fresh answer, delete the file.

## Monitoring

- `/ops`: requests per kind (p50/p95), LLM calls, tokens and cost per kind and per task, degraded
  calls, review backlog.
- Logs are JSON with a `request_id`, so you can follow one turn across the API, agent and ledger.
- Langfuse, if configured: one trace per turn.

## Degraded mode

| Down | Behaviour |
|---|---|
| LLM | intent from rules (asks back when unsure), RCA sections quoted, agent replaced by read-only checks plus reference pages, no drafting |
| Embeddings | keyword search; the reply says "these results match keywords only" |
| Shop | no checks or fixes; the commands are shown to run by hand |
| Agent server | `/incidents/{id}/open` returns 503; REST still works |

Every degraded call is logged in the `ledger` and counted on `/ops`.

## Troubleshooting

- **First search is slow (~12 s).** Vertex opens a new connection. The pool is kept alive for
  10 min and warmed at startup.
- **Seed looks stuck.** Same cause, first embedding call. Wait (timeout is 30 s, with retries).
- **Same answer as before.** It's the LLM cache. Delete the entry to get a fresh one.
- **No new alert in the UI.** The incident was already opened. Check
  `GET /incidents?status=all`.
- **Checks say not reachable in k8s mode.** Start with `SHOP_MODE=k8s make demo` and run
  `make cluster` (it writes the kubeconfig).
- **Demo starts half broken.** Run `make reset` or `make reset-k8s`.
- **Fresh start.** `docker compose --profile demo down -v && make demo`.
