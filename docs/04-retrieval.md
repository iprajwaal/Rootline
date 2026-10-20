# Retrieval

[`retrieval/search.py`](../src/rootline/retrieval/search.py), [`retrieval/suggest.py`](../src/rootline/retrieval/suggest.py), [`retrieval/issue.py`](../src/rootline/retrieval/issue.py), [`retrieval/passages.py`](../src/rootline/retrieval/passages.py).

## Search

`Searcher.search(query)` tries these in order:

1. **ID.** Anything that looks like a doc ID (`SOP-DB-001`, `RCA-2025-014`, `SOP-B-1A2B3C`, any
   case) is an exact lookup. Nothing else is returned.
2. **Title.** Trigram similarity ≥ 0.6. If the next title is within 0.1, both are returned and
   marked ambiguous.
3. **Hybrid**, run separately for the RCA and SOP lists, each on its own partition:
   - top 30 chunks by vector, top 30 by full-text;
   - reciprocal rank fusion (k = 60);
   - per doc, keep it if similarity ≥ 0.64, or ≥ 0.58 with evidence. Evidence means two
     matching words, a rare identifier (counts double), or a named service plus a word;
   - × 1.15 for a matching service, × 0.8 if superseded;
   - drop anything more than 0.1 below the best similarity;
   - collapse near-copies into the canonical doc and keep 5;
   - top two within 0.02 means ambiguous; nothing left means `nothing_relevant`.

If embeddings are down, `mode` is `lexical` and only the evidence rule applies.

Thresholds come from a dev-only sweep (`make experiments`). The right doc scores 0.62 to 0.73;
unrelated queries score at most 0.54. The score is flat for 0.56 to 0.60 with evidence, so we
picked the middle.

A rare identifier is anything with mixed case, an underscore, or letters next to digits
(`OOMKilled`, `PoolTimeout`, `psycopg_pool`). It's worth more than a common word like "orders".

## Suggest

`Suggester.suggest(text)` is used by `/suggest`, by `/incidents`, and by the chat.

Reading the issue (`retrieval/issue.py`):
- strip ANSI, stack frames, and INFO/DEBUG lines when there are louder ones;
- scrub timestamps, request/trace IDs, UUIDs, IPs, pod hashes (`orders-api-7d9f8b6c5-x2klm` →
  `orders-api`), hex and long numbers. Short numbers like exit code 137 stay;
- pull out alerts, services and error signatures;
- the query is the alert names plus one line per error. An alert no doc mentions is left out of
  the query; it would only drag in every doc about that service.

Ranking:
- × 1.3 if the doc lists the fired alert, × 1.15 if it has the same error (compared by class name,
  so `psycopg_pool.PoolTimeout: ...` matches `PoolTimeout`);
- if any doc shares the alert or error, docs that only share words are dropped;
- if none does and the issue has a named error, the list is `nothing_relevant`. A new error goes
  to the agent instead of to a loosely similar SOP;
- keep 3 per list.

Every suggestion gets a `why` built from what it shares, e.g. "Procedure with the same alert
OrdersDbPoolExhausted, error PoolTimeout and service orders-api."

## Passages for RCA answers

`top_passages` takes the 2 closest RCAs and picks 12 passages, fusing vector and keyword rank.
With only a few docs involved, those chunks are ranked exactly in a materialized CTE instead of
through HNSW. HNSW filters after taking its nearest neighbours, which dropped good passages and
changed results between builds. Ties break on `chunk_id`, so the same question always builds the
same prompt.

## Latency (warm)

- ID lookup ~70 ms
- hybrid search 50 to 170 ms
- alert with a known cause, full chat turn: 36 ms; 1.7 s on the cluster including opening the
  thread

The first embedding call after ~10 min idle pays ~12 s for a new Vertex connection.

## Notes

- New errors used to match old SOPs. A read-only DB error (`psycopg.errors.ReadOnlySqlTransaction`)
  suggested SOP-DB-001 at 0.65 on shared words. Two fixes: the error regex now knows dotted library
  errors, and an unseen named error now returns `nothing_relevant`. Covered by
  `test_named_error_no_document_has_seen_says_nothing_relevant`.
