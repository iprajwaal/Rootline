# API

## REST (`:8000`, [`api/app.py`](../src/rootline/api/app.py))

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | |
| POST | `/documents` | multipart `file`, optional `source_key` (defaults to `uploads/<filename>`). 415 on unknown format |
| DELETE | `/documents?source_key=` | 404 if nothing has that key |
| GET | `/search?q=` | max 2,000 chars |
| GET | `/docs/{doc_id}` | HTML with `Accept: text/html`, JSON otherwise |
| POST | `/suggest` | `{"text": "..."}`, max 100k chars |
| POST | `/incidents` | Alertmanager webhook, version 4 |
| GET | `/incidents?status=open\|all&limit=` | |
| POST | `/incidents/{id}/open` | creates the chat thread; only the first call does |
| GET | `/review` | drafts and review items |
| POST | `/review/{id}/approve`, `/reject`, `/done` | redirect back to `/review` |
| POST | `/review/{id}/type` | form `doc_type=rca\|sop`; moves the chunks to that partition |
| GET | `/ops?days=` | 1 to 90 days |

Ingest returns `{"source_key", "action", "doc_ids"}`. `action` is one of `created`, `updated`,
`unchanged`, `alias`, `review_only`, `deleted`.

`/search` response:

```json
{
  "query": "redis connection refused",
  "mode": "hybrid",
  "rca": {"hits": [...], "ambiguous": false, "nothing_relevant": false},
  "sop": {"hits": [{"doc_id": "SOP-CACHE-001", "doc_type": "sop", "title": "...",
                    "status": "active", "version": 1, "similarity": 0.69,
                    "evidence": ["matched: redis, connection, refused", "service: cache",
                                 "section: card", "similarity 0.69"],
                    "similar_copies": 0, "linked": [...]}],
          "ambiguous": false, "nothing_relevant": false},
  "did_you_mean": [],
  "latency_ms": 168.5
}
```

- `mode` is `id`, `title`, `hybrid`, or `lexical` when embeddings are down.
- The `rca` and `sop` lists never mix types.

`/suggest` returns the same two lists, plus `issue` (the alerts, services and errors it read)
and a one-line `why` on each suggestion.

## Agent server (`:2024`)

This is the standard LangGraph API, served by Aegra. The graph id is `agent`. With
`langgraph_sdk`:

```python
c = get_sync_client(url="http://localhost:2024")
t = c.threads.create(metadata={"graph_id": "agent"})["thread_id"]
c.runs.wait(t, "agent", input={"messages": [{"role": "human", "content": "steps for db pool"}]})
# answer an approval card
c.runs.wait(t, "agent", command={"resume": {"decisions": [{"type": "approve"}]}})
```

A run paused for approval returns `__interrupt__` with a HITL request: the action name, the
stored command, and the dry-run result. Only `approve` and `reject` are allowed. Edit is
deliberately off: what runs is the stored command.

## CLI

```bash
rootline ingest PATH... [--sync]    # --sync deletes docs whose file is gone
rootline delete SOURCE_KEY
```
