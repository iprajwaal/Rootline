# Ingestion

Entry points: `rootline ingest`, `POST /documents`, the `seed` service, and approved drafts. They
all call `Store.ingest(source_key, data)` in [`ingest/store.py`](../src/rootline/ingest/store.py).

## Steps

1. If the raw sha256 matches a stored version that already has vectors, return `unchanged`.
   Nothing is parsed or sent anywhere.
2. Save the raw bytes under `data/raw/<sha256>` (temp file + rename).
3. Analyse outside the transaction ([`ingest/analysis.py`](../src/rootline/ingest/analysis.py)): parse, clean, redact, classify,
   split, extract, facts, chunk, embed.
4. Open a transaction and take advisory locks on the source key, raw hash and content hash. Check
   `unchanged` again in case another process got there first.
5. If the content is the same as another doc under a different name, record an alias and stop.
6. If the type couldn't be determined, create a `review_only` item. It is not searchable.
7. Otherwise write a new version: version row, chunks, steps, links, review items and dup cluster.
   Parts the file no longer has are tombstoned.

## Parsing

| Format | Parser |
|---|---|
| md | markdown-it + front-matter |
| txt, eml | own; headings guessed from short lines |
| html | BeautifulSoup (wiki exports) |
| json | Slack export, one block per message |
| pdf | pdfplumber; drops running headers, footers and page numbers |
| docx | python-docx; drops struck-through text |

Control characters and NUL bytes are stripped from all text ([`core/text.py`](../src/rootline/core/text.py)).

A doc is greenfield if it's a filled template with its own ID. Anything else, including a template
with `TBD` as its ID, gets an ID derived from its source key, e.g. `SOP-B-1A2B3C`. The same file
always gets the same ID.

## Redaction

[`ingest/redaction.py`](../src/rootline/ingest/redaction.py). Runs before classification, so secrets never reach the LLM, the cache, a
chunk or a log. The chat guard runs the same function on user input.

- Our own regex rules come first: private keys, AWS/Google/GitHub/Slack tokens, JWTs, API keys,
  passwords, private IPs, internal hosts, and high-entropy strings. Entropy is measured on the
  longest unbroken run, so slugs like `inc-2025-06-03-orders-api-outage` are left alone.
- detect-secrets runs after them (format and keyword plugins only).
- Matches become typed placeholders like `[REDACTED:PASSWORD]`.
- Docs with secrets get a `secrets_redacted` review item.

## Classification

[`ingest/classify.py`](../src/rootline/ingest/classify.py), in order:

1. Fewer than 5 real words: `unknown`, held for review.
2. Template structure: type known, confidence 1.0.
3. Rules score each block as story or procedure. Mixed and empty docs are decided here.
4. Logistic regression (TF-IDF plus 3 rule features, trained on the labelled corpus at startup).
5. If confidence is below 0.7, ask Gemini. Its answer only keeps its confidence if it agrees with
   step 4; otherwise it's capped below 0.7.
6. Below 0.7: `low_type_confidence` review item.

Mixed docs (at least 25% procedure blocks, with a clean incident part and a clean procedure
part) are split into `<key>#rca` and `<key>#sop`, linked with `split_from`.

## Extraction

- Greenfield ([`ingest/template_extract.py`](../src/rootline/ingest/template_extract.py)): no LLM. Sections, the step table, rollback, five
  whys, fishbone.
- Brownfield ([`ingest/extract.py`](../src/rootline/ingest/extract.py)): one Gemini call, then checked against the source:
  - step instructions must fuzzy-match at ≥ 90 and appear in source order;
  - commands must match exactly (fuzzy would accept `DB_POOL_MAX=500` for `=50`);
  - RCA facts must match at ≥ 85, and at least 80% of them must be found.

  If the check fails, the doc is stored with `verified = false` and an `extraction_unverified`
  review item.

Facts ([`ingest/facts.py`](../src/rootline/ingest/facts.py)) are all regex: services from catalog names and aliases, catalog alerts,
error signatures, dates, mentioned doc IDs, `Supersedes`.

## Chunking and embeddings

- One `card` chunk per doc: title, type, services, a short summary and the error lines.
- Then one chunk per section, packed up to 1,600 chars. Tables split between rows with the header
  repeated, so a step is never cut.
- Each chunk starts with a header like `[SOP · SOP-DB-001 v1 · orders-api] steps`.
- Embeddings: Vertex `text-embedding-005`, 768 dims, batches of 100, cached by text hash. If
  Vertex is down, chunks are stored without vectors and filled in on the next ingest.

## Duplicates, versions, deletes

| Case | Result |
|---|---|
| same bytes again | `unchanged` |
| same file from two processes at once | locks + re-check: one writes, the other is `unchanged` |
| edited file (same source key) | version n+1, chunks and steps swapped in the same transaction |
| same content, new name | alias |
| near copy | 10 nearest cards by embedding, then 4-word shingle containment with boilerplate removed. ≥ 0.45 joins the cluster; 0.35 to 0.45 becomes a `possible_duplicate` review item |
| deleted | status `deleted`, chunks and steps removed. If an alias still has the same text, the doc moves to that key |

Search shows only the canonical doc of a cluster. The canonical doc is chosen by: greenfield,
then active, then newest, then verified, then confidence.

A doc with `Supersedes: SOP-X` marks SOP-X as superseded. SOP-X stays searchable but ranks below
its replacement.

We tried embeddings for near-dup detection first. Copies scored as low as 0.85 and unrelated docs
on the same topic as high as 0.93, so no threshold works. Shingles separate them once template
boilerplate is removed.

Tests: [`tests/test_ingest_idempotency.py`](../tests/test_ingest_idempotency.py) (twice, 8 parallel processes, edit, delete),
[`tests/test_duplicates.py`](../tests/test_duplicates.py), [`tests/test_redaction.py`](../tests/test_redaction.py).
