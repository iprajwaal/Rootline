# Design notes

These are my notes on how Rootline ended up the way it is: what I decided, what I changed along
the way, and why.

## Where I started

My first reading of the brief was "RAG over runbooks". The second reading changed that. The
brief keeps coming back to the same failure: an engineer under pressure opens the wrong document,
or follows a step that's slightly off, and makes the outage worse. A system that answers
fluently but sometimes picks the lookalike is worse than no system. So I set one rule before
writing any code: **be exact where you can, say so when you can't, and never guess quietly.**

I wrote down what every reply has to carry, and most later decisions fell out of it:
- a badge saying whether it's an RCA or an SOP;
- the doc ID, version and age, with a link;
- a citation for every claim, and no claim without one;
- the evidence it matched on;
- what it did, step by step;
- what you can do next.

I had three open questions I couldn't answer from the brief:
- **Were queries provided?** None came with it, so I wrote 118 myself.
- **What does "isolation" mean?** I read it as RCAs and SOPs never mixing, and enforced that
  physically.
- **Will reviewers have an LLM key?** I assumed maybe not, so everything that can run without a
  model does.

## One database

I started with the usual split: Postgres for metadata, a vector store for search. I dropped it
quickly. The brief cares most about ingestion being correct: no duplicates when the same file
arrives twice or at the same time, edits replacing old versions, deletes disappearing
everywhere. That is far easier when the document, its versions, its chunks and its steps change
in **one transaction**. pgvector, full-text search and trigram matching all live in Postgres, and
a few thousand documents is a small index.

RCA and SOP chunks sit in separate partitions, each with its own index. An RCA query can't return
an SOP chunk even if someone forgets a filter. Concurrent ingests are serialized with advisory
locks on the file, the raw hash and the content hash.

## Getting messy documents in

The greenfield template is easy. The brownfield pile is the real work: wiki exports, email
threads, Slack dumps, PDFs, Word files, copies under other names, and pages that mix an incident
write-up with a runbook.

**Redact first.** Secrets are stripped before anything else runs, so no secret ever reaches a
model, the cache, a chunk or a log. That uses regex plus detect-secrets. I didn't consider an
LLM for this; you'd be sending the secret to the model to ask whether it's a secret.

**Deciding the type.** The obvious move is to let an LLM classify every file. I didn't, for two
reasons. It costs a call per file, and its confidence means nothing. So it's a cascade:
1. The template, if the file has one.
2. Rules on story-like vs procedure-like sections.
3. A small logistic regression.
4. The LLM, only when those are unsure.

Anything still below 0.7 goes to a person. The [experiment](eval/runs/20261020T100733Z-experiments/report.md) later backed this up. On the five hard
documents, the LLM alone got 4 right but was confidently wrong once. The cascade also got 4
right, sent 3 to review, was never confidently wrong, and needed 3 calls instead of 29.

**Mixed files** are split into an RCA part and an SOP part, linked to each other. **Brownfield
steps** are extracted by the LLM, then checked against the source text: instructions must match
closely and in order, and commands must match exactly. A fuzzy match would let `DB_POOL_MAX=500`
pass for `=50`. If the check fails, the doc is stored as unverified and flagged for review.

**Duplicates** are where I changed my plan. I planned MinHash plus embedding similarity. Then I
measured it on the corpus: true copies scored as low as 0.85, and different documents on the
same topic as high as 0.93. No threshold separates those. Plain word shingles do, once the shared
template boilerplate is removed. Above 0.45 is a copy; 0.35 to 0.45 goes to review. Search only
shows the canonical document of a cluster, with "N similar".

## Finding the right document

Exact lookups go first. An ID returns that document and nothing else. A title match comes next.
Only after that does hybrid search run, keyword and vector fused, separately for RCAs and SOPs.
A document has to clear a similarity floor, a lower one if it also shares words or a service. If
the top two are too close, both are shown as ambiguous rather than one being picked.

I tuned the floors with a [sweep](eval/runs/20261020T100733Z-experiments/report.md) on dev queries only: 0.64 on meaning alone, 0.58 with
supporting evidence. To be honest about hybrid: it isn't clearly better than vector alone here,
since the intervals overlap. I kept it because it's never worse on any slice, and the keyword
half keeps search working when the embedding model is down.

**Embeddings: I changed my mind once.** I started with local bge-small embeddings: free, fast
per query, and nothing leaves the machine. In practice the seed took about a minute on CPU inside
Docker, and that grows with the corpus. I moved to Vertex `text-embedding-005` and put two
safeguards around it:
- every vector is cached on disk and committed, so a fresh clone needs no calls;
- search falls back to keywords if Vertex can't be reached.

Then I re-ran the threshold sweep. The trade-off I accepted: Vertex takes about 12 seconds to
open a new connection, so the first query after a long idle is slow.

I'd also planned a reranker. I never found a query set where it would have helped, so it isn't
there.

## Live incidents

An alert comes in with its real error line. I strip the log noise (timestamps, pod hashes, IDs)
and pull out the alert name, the services and the error signatures. Documents that share the
fired alert or the exact error rank above ones that only share words. Each suggestion gets a
one-line "why", built from what it shares. No model writes that line, so it can't be wrong about
the data.

One change came late. I added a fault with no documents (the database left read-only after
maintenance) and Rootline confidently suggested the connection-pool SOP: same service, similar
words, similarity 0.65. Two things were missing. The error pattern didn't recognise
`psycopg.errors.ReadOnlySqlTransaction`, and nothing said that **a named error no document has
seen is a new problem**. Fixing both made it say "nothing relevant", which is the honest answer.

## Chat

Intent uses the same idea as classification: rules first, the model when they're unsure. This
is where the evidence surprised me most. The LLM alone was the most accurate (0.70), but it was
confidently wrong on 26% of messages, and said 0.9 or higher on every one of its wrong answers.
The cascade is slightly less accurate (0.66, inside the noise) but confidently wrong only 7% of
the time. When it's unsure, it asks back. During an incident, a question back costs seconds; a
wrong route costs the wrong document.

SOP steps never go through a model. They come word for word from the stored rows. RCA questions
do go through one, because a summary needs it. Every sentence of the answer must cite a passage
it was given, share most of its words with that passage, and contain no number the passage
doesn't. A sentence that fails is dropped.

## When nothing covers the issue

This part changed the most, in stages.

1. My first plan kept this out of scope. Rootline would say "not covered", show public reference
   pages and run a few read-only checks. Automatic fixing was something I deliberately left out.
2. Testing the disk-full fault showed how little that helps. The engineer is left exactly where
   they started. So I added an agent with read-only tools: health, catalog checks, search, read an
   SOP, and dry-run a fix.
3. The first version of the agent applied the fix itself and reported success. That was wrong:
   the engineer can't see what changed, and nobody approved it. I took that away. The agent now
   only proposes steps. A step that changes something must be a known catalog command, and its
   dry run must show it helps. Anything else is dropped before you see it.
4. In rehearsals, copying commands out of the chat was clumsy. So `run step N` came back, with
   rules:
   - it runs the stored command, never text from the chat;
   - it dry-runs first;
   - it pauses on an approval card;
   - it runs under a separate Kubernetes account that is the only one allowed to change anything.

   I had planned an "edit" button on that card and removed it. An edited command isn't the one
   that was tested.
5. At first, the write-up of the incident was drafted automatically once health was back. That
   filled the review queue with drafts nobody asked for. Now it asks first, and nothing becomes
   searchable until a reviewer approves it.

The result: the library grows from real incidents, and only through people.

## A shop to break

To test any of this I needed something that fails realistically. I started with [ShopLite](sandbox/shoplite), a
small simulated shop. It's fast and deterministic, and the automated tests still use it. But a
simulation only fails the ways you program it to. So I put a real version on a local [kind
cluster](cluster): an orders API, a worker, Postgres, Redis, Redpanda, and an alerter that probes health
and posts alerts carrying the real log line.

It paid off quickly. Under load, receipts filled the disk with no fault injected. And the pool
alert fired before any error had been logged. Neither would have shown up in the simulation.

I looked at Cloud Run. But scaling replicas up and down, rolling back, or running a command
inside the database aren't one-command operations there, and every reviewer would need a GCP
project. kind runs on a laptop.

## Measuring it

No query set came with the brief, so I wrote [118 queries](eval/queries/queries.jsonl). They cover every kind of request (ID,
title, service, error, description, alert, stack trace, chat, ambiguous, no answer) and are
tagged by whether the right document is greenfield or brownfield. A seeded split locked 34 away,
with the file's hash recorded. All the tuning used the other 84.

Every number comes with a bootstrap interval, because these groups are small. LLM answers and
embeddings are [cached and committed](cache), so a run can be repeated exactly and costs nothing.

On dev ([`eval/runs/20261020T124924Z-dev/report.md`](eval/runs/20261020T124924Z-dev/report.md)):

| | greenfield | brownfield |
|---|---|---|
| RCA exact@1 | 0.97 | 0.73 [0.54, 0.89] |
| SOP exact@1 | 0.83 | 0.96 |

| | |
|---|---|
| Wrong-type results | 0 |
| Clean vs noisy input | 0.97 vs 0.97 |
| SOP steps shown word for word | 1.00 |
| RCA answers with valid citations | 8 of 8 |
| Intent accuracy | 0.66 (confidently wrong 0.07) |

How much I trust these:
- **The direction, yes. The second decimal, no.** The groups are 10 to 30 queries.
- **I wrote both the documents and the queries.** The brownfield mess comes from a script, and
  real legacy docs will be messier.
- **Everything was tuned on dev**, so these numbers flatter it. The held-out 34 are the real test,
  and I haven't run them yet.
- **A few dev misses are mistakes in my own labels.** I left them as recorded rather than fix the
  labels after seeing results.

## Cost and response time

Prices: Gemini Flash-Lite at $0.30/M input and $2.50/M output tokens. Costs are ledger averages;
times are from the warm stack.

| Request | LLM calls | Cost | Time |
|---|---|---|---|
| Find by ID or title | 0 | 0 | ~10 to 70 ms |
| Search by description or error | 0 | ~$0.000002 (one embedding) | 50 to 170 ms |
| Alert with a known cause: RCA, SOP and steps | 0 | ~$0.000002 | under 2 s |
| RCA question | 1 | $0.0011 | ~3.6 s |
| Investigation, nothing covers it | 4 to 7 | $0.003 to $0.005 | 6 to 11 s |
| Write-up | 1 | $0.003 | 3 to 4 s |

The common case, a known incident, costs nothing, and that was the point. A rough estimate for
400 engineers, assuming about 2,000 chat turns a day, comes to under a dollar a day. Ingesting a
few thousand documents costs a few dollars once.

## What I cut, and what I'd do next

1. Run the held-out queries once and publish the result next to dev.
2. A CI gate. `make check` runs 809 offline [tests](tests) today, including retrieval per fault,
   isolation, secret canaries, steps and citations. But nothing runs them on every push or
   compares the eval against a stored baseline.
3. Follow-ups: "why did this happen last time?" right after an SOP should find that SOP's
   incident. It doesn't yet.
4. The scenario runner can't answer approval cards yet, so scenario s1 needs updating.
5. A keep-alive for the embedding connection, to remove the slow first query.
6. A scale test with a few thousand documents. The near-duplicate check reads up to 1,000 texts
   per ingest and will need hashing to scale.
7. Auth on the review page and on uploads.
8. Exec-based fixes like `psql`: Rootline keeps the read-only account away from them, but
   Kubernetes RBAC doesn't.
9. Intent is the weakest number. Most misses are short lookups that get asked back. I'd start
   there.
