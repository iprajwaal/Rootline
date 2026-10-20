# Learning loop

[`agent/drafts.py`](../src/rootline/agent/drafts.py), [`review.py`](../src/rootline/review.py), and the `draft`/`decline` nodes.

## Offer

Nothing is written up automatically. Once health is ok again (after an approved fix, or after
`done` and a recheck), the reply asks:

> Should I write this incident up as a new SOP and RCA? ... Answer yes or no.

If an SOP was followed, the offer is for a new version of that SOP plus a new RCA. `yes` (or
"sure", "go ahead", "draft the SOP and RCA") writes drafts. `no` writes nothing. Both are matched
by rules.

## Drafting

One Gemini call (`draft-v3`). Inputs: the chat transcript with timestamps, the services and
alerts in it, the SOP that was followed (if any), and the topic words already used in SOP IDs.
Output: `DraftLlm`, with the SOP sections (purpose, when to use, prerequisites, steps, checks,
rollback, what changed) and the RCA sections (problem, impact, trigger, timeline, root causes,
resolution, action items).

`Drafter.save` then applies these rules:
- a command is kept only if it appeared in the chat or in the followed SOP; dropped commands are
  listed in the reply;
- a followed SOP keeps its ID and gets version n+1 with a revision-history entry;
- a new SOP gets `SOP-<TOPIC>-<next number>`, reusing an existing topic where it fits (so
  `SOP-DB-002`, not `SOP-DATABASE-001`);
- a new RCA gets `RCA-<year>-<next number>`;
- a second write-up in the same chat replaces the earlier drafts;
- the SOP links to the RCA, and both link to any past RCA in focus.

Drafts are rendered in the greenfield template, so once approved they ingest like any templated
doc: no LLM call, and the steps come from the step table.

## Review

`/review` lists open drafts and other review items.

- Approve: claims the item (only the first approve counts), then ingests it as
  `drafts/<doc_id>.md` with actor `reviewer`. If the ingest fails, the item goes back to open.
- Reject: kept for the record, never indexed.
- Fix type: for docs the classifier wasn't sure about. Moves the chunks to the right partition.

Drafts aren't searchable until approved ([`tests/test_learning.py`](../tests/test_learning.py)).

## Example run (kind, `db_read_only`)

| Turn | LLM calls | Time |
|---|---|---|
| alert opened: no doc, agent runs health, `db_read_only` check, `try_fix allow_db_writes` | 4 to 6 | 6 to 11 s |
| `run step 2` + approve | 0 | 0.2 s |
| `yes`: SOP-DB-002 and RCA-2026-014 queued | 1 | 3 s |
| after approval, same alert again: both docs straight from suggest | 0 | 1.7 s |
