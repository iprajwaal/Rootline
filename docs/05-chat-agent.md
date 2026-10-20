# Chat and agent

Graph: [`agent/graph.py`](../src/rootline/agent/graph.py) (diagram in [00-flow.md](00-flow.md)). Each node adds a step to the trace,
which the UI shows as the step log. Each turn ends with a footer:
`steps: ... · LLM calls N · M ms`.

## Guard

[`agent/guard.py`](../src/rootline/agent/guard.py) runs on every message:
- caps it at 20 KB, keeping the head and the last 4 KB (Python puts the exception at the end);
- applies NFKC and strips invisible characters;
- redacts secrets. The cleaned text replaces the message in the thread;
- flags prompt-injection phrases. Flagged text is only logged; nothing in the graph lets a
  message run commands, and every prompt wraps user text in data tags.

## Intent

[`agent/understand.py`](../src/rootline/agent/understand.py). Intents: `find`, `suggest`, `ask_sop`, `ask_rca`, `troubleshoot`,
`unclear`, plus four that only rules produce: `run_step`, `draft`, `decline`, `recheck`.

1. Rules look at doc IDs, pasted traces, logs and alerts, catalog names, and cue words (find, how,
   why, live). A guess at ≥ 0.7 is used as is.
2. Otherwise Gemini is asked (`intent-v2`). Its confidence isn't calibrated: on dev it said ≥ 0.9
   on every wrong answer. So it only decides alone when the rules had no lean. If the rules lean
   one way, both must agree.
3. If they still disagree, the turn is `unclear` and the reply asks which reading was meant.

"yes" and "no" are only read as answers while a write-up offer is open.

Routing (`route`) adds three overrides:
- `troubleshoot` with an SOP in focus goes to `ask_sop`;
- during an investigation, a follow-up that names nothing new goes back to the agent;
- `unclear` with a service, error or alert named, where one reading is a lookup, goes to `find`
  instead of asking back.

## Focus

`Focus` in [`agent/state.py`](../src/rootline/agent/state.py) is saved with the thread checkpoint:

```python
sop, sop_version, rca   # what the conversation is about
issue                   # the issue as first described
plan                    # steps the agent gave, with their source
failing                 # parts seen failing
drafts                  # review ids of drafts from this chat
offered                 # waiting for yes/no on a write-up
```

This is what lets "steps", "run step 2" or "done" work without repeating the doc ID.

## Answers

- `find`: cards with an `[RCA]`/`[SOP]` badge, ID, version, age, services, why it matched, and a
  link to `/docs/<id>`.
- `suggest`, known cause: top RCA and SOP, with the SOP's steps inline. 0 LLM calls.
- `ask_sop`: steps and rollback straight from `sop_steps`, as a checklist. Never paraphrased.
  0 LLM calls.
- `ask_rca` ([`agent/answer.py`](../src/rootline/agent/answer.py)): 12 passages from the 2 closest RCAs go to Gemini, with the rule
  "every sentence cites a source". `checked()` then drops any sentence that:
  - cites nothing it was given,
  - shares less than 60% of its words with the cited passages (5-letter stems), or
  - contains a number that isn't in them.

  If nothing survives, the reply says the closest RCAs don't answer the question and links them.
  Without an LLM, it quotes the closest sections.

## The agent

The agent runs when `suggest` finds nothing in either list, or on `troubleshoot` with no SOP in
focus. Code is in [`agent/troubleshoot.py`](../src/rootline/agent/troubleshoot.py) and [`agent/toolbox.py`](../src/rootline/agent/toolbox.py).

Tools (all read-only):

| Tool | |
|---|---|
| `check_health` | per-component health |
| `run_check` | a catalog check: logs, `db_connections`, `cache_ping`, `queue_lag`, `disk_usage`, `db_read_only`, ... |
| `search_docs` | internal RCAs and SOPs |
| `read_sop` | one SOP in full |
| `search_references` | public reference pages, labelled `[REF]` |
| `try_fix` | dry run of a catalog action |

It gets at most 6 tool calls, then one conclusion call (`conclude-v2`). The conclusion's claims
are checked the same way as RCA answers. Its steps go through `vetted()`:
- the command must be a catalog check or action, or appear in a doc the agent read;
- if the command changes something, its dry run must show it helps.

A step that fails either rule is dropped entirely.

The reply has the findings, numbered steps (command, what to expect, and whether it changes
anything), current health, which docs were checked (or "No internal SOP or RCA covers this"), and
a list of every tool call with its result.

## Running steps

`run step N` (or `run rollback step N`) runs a step from the plan or the SOP in focus. The command
always comes from the stored step, never from the chat text.

- Checks run right away with the read-only account.
- Fixes are dry-run first. If the dry run would change nothing, the fix isn't offered. Otherwise
  the graph pauses on an approval card (command, dry-run result, target). On approve, it runs with
  the fixer account and shows the command, its output, the rollout status and health. On reject,
  it prints the command for you to run yourself.
- Steps with nothing to run are shown as manual steps.

When an approved fix brings health back, or `done` triggers a recheck that finds it healthy, the
reply asks whether to write the incident up. See [06-learning-loop.md](06-learning-loop.md).

## LLM calls per turn

| Turn | Calls |
|---|---|
| find, ask_sop, known-cause suggest, run_step, recheck, decline | 0 |
| intent the rules aren't sure about | +1 |
| ask_rca | 1 |
| investigation | 4 to 7 |
| write-up | 1 |
