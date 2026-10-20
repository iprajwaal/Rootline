# Eval report: dev

84 queries from `eval/queries/dev.jsonl` · test file sha256 222436e60fddbdb5… · LLM gemini-3.5-flash-lite · embeddings text-embedding-005 · 2026-10-20 09:59 UTC.
Numbers in brackets are 95% bootstrap intervals over the queries. With groups this small they are wide; treat a difference inside them as noise.

## Retrieval by list and origin

| List / origin | n | exact@1 | MRR | recall@5 |
| --- | ---: | --- | --- | --- |
| rca/greenfield | 29 | 0.97 [0.90, 1.00] | 0.97 [0.90, 1.00] | 0.97 [0.90, 1.00] |
| rca/brownfield | 26 | 0.73 [0.54, 0.89] | 0.79 [0.64, 0.92] | 0.85 [0.69, 0.96] |
| rca/all | 55 | 0.85 [0.76, 0.94] | 0.88 [0.80, 0.95] | 0.91 [0.84, 0.98] |
| sop/greenfield | 30 | 0.83 [0.70, 0.93] | 0.85 [0.73, 0.95] | 0.87 [0.73, 0.97] |
| sop/brownfield | 24 | 0.92 [0.79, 1.00] | 0.92 [0.79, 1.00] | 0.92 [0.79, 1.00] |
| sop/all | 54 | 0.87 [0.78, 0.94] | 0.88 [0.78, 0.95] | 0.89 [0.80, 0.96] |

## Retrieval by kind of query

| Kind | n | exact@1 | MRR |
| --- | ---: | --- | --- |
| alert | 12 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| ambiguous | 3 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| chat_rca | 8 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| chat_sop | 8 | 0.88 [0.62, 1.00] | 0.94 [0.81, 1.00] |
| description | 26 | 0.77 [0.58, 0.92] | 0.79 [0.61, 0.92] |
| error | 18 | 0.94 [0.83, 1.00] | 0.97 [0.92, 1.00] |
| id | 5 | 0.80 [0.40, 1.00] | 0.80 [0.40, 1.00] |
| service | 12 | 0.75 [0.50, 1.00] | 0.79 [0.54, 1.00] |
| stack_trace | 6 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| title | 11 | 0.73 [0.46, 0.91] | 0.73 [0.46, 0.91] |

## Safety of the lists

- Wrong-type rate (a list showing another type): 0.00 [0.00, 0.00]
- Abstain precision 0.545 · recall 0.8 (22 abstained, 15 should have)
- Log noise: exact@1 0.97 [0.92, 1.00] clean vs 0.97 [0.92, 1.00] noisy, delta 0.00 [0.00, 0.00] over 36

## Intent

| Origin | n | accuracy | confident-wrong |
| --- | ---: | --- | --- |
| greenfield | 39 | 0.69 [0.54, 0.82] | 0.05 [0.00, 0.13] |
| brownfield | 31 | 0.61 [0.42, 0.77] | 0.10 [0.00, 0.23] |
| none | 14 | 0.64 [0.43, 0.86] | 0.07 [0.00, 0.21] |
| all | 84 | 0.66 [0.55, 0.75] | 0.07 [0.02, 0.13] |

Misses (expected → got): find → unclear ×18, find → ask_sop ×4, suggest → unclear ×4, find → ask_rca ×1, find → suggest ×1, troubleshoot → unclear ×1

## Answers

- SOP steps word for word: 1.00 [1.00, 1.00] over 6 procedures shown; 2 questions got two equal SOPs to choose from
- RCA answers with citations: 8 of 8; citations valid 1.00 [1.00, 1.00]; cite the right incident 0.88 [0.62, 1.00]

## Latency and cost per request kind (full chat turn)

Calls served from the committed LLM cache cost nothing and return fast, so the share from cache says how much of a row is the live cost.

| Route | n | p50 ms | p95 ms | LLM calls | from cache | input tokens | output tokens | USD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ask_rca | 11 | 63 | 70 | 0.82 | 1.0 | 0 | 0 | 0.000000 |
| ask_sop | 15 | 51 | 121 | 0.13 | 1.0 | 0 | 0 | 0.000000 |
| find | 15 | 51 | 66 | 0.67 | 1.0 | 0 | 0 | 0.000000 |
| suggest | 18 | 74 | 20118 | 0.39 | 0.0 | 446 | 15 | 0.000172 |
| unclear | 25 | 30 | 34 | 1 | 1.0 | 0 | 0 | 0.000000 |
