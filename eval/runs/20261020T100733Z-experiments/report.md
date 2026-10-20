# Boundary experiments and calibration (dev only)

Every number comes from `eval/queries/dev.jsonl` or the labelled corpus. The held-out test set is not read here. Brackets are 95% bootstrap intervals.

## Search: keyword vs vector vs hybrid

| Method | origin | exact@1 | MRR | recall@5 | abstain P | abstain R | wrong type |
| --- | --- | --- | --- | --- | ---: | ---: | --- |
| keyword | greenfield | 0.86 [0.76, 0.95] | 0.89 [0.81, 0.96] | 0.92 [0.83, 0.98] | 0.714 | 0.667 | 0.00 [0.00, 0.00] |
| keyword | brownfield | 0.78 [0.66, 0.88] | 0.85 [0.76, 0.93] | 0.92 [0.84, 0.98] | 0.714 | 0.667 | 0.00 [0.00, 0.00] |
| keyword | all | 0.83 [0.75, 0.89] | 0.87 [0.82, 0.93] | 0.92 [0.87, 0.96] | 0.556 | 0.667 | 0.00 [0.00, 0.00] |
| vector | greenfield | 0.88 [0.80, 0.95] | 0.90 [0.81, 0.97] | 0.92 [0.83, 0.98] | 0.75 | 0.8 | 0.00 [0.00, 0.00] |
| vector | brownfield | 0.82 [0.70, 0.92] | 0.85 [0.74, 0.94] | 0.88 [0.78, 0.96] | 0.667 | 0.8 | 0.00 [0.00, 0.00] |
| vector | all | 0.85 [0.78, 0.92] | 0.88 [0.81, 0.93] | 0.90 [0.84, 0.95] | 0.545 | 0.8 | 0.00 [0.00, 0.00] |
| hybrid | greenfield | 0.90 [0.81, 0.97] | 0.91 [0.83, 0.97] | 0.92 [0.83, 0.98] | 0.75 | 0.8 | 0.00 [0.00, 0.00] |
| hybrid | brownfield | 0.82 [0.70, 0.92] | 0.85 [0.75, 0.94] | 0.88 [0.78, 0.96] | 0.667 | 0.8 | 0.00 [0.00, 0.00] |
| hybrid | all | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.90 [0.84, 0.95] | 0.545 | 0.8 | 0.00 [0.00, 0.00] |

## Threshold sweep (hybrid)

`alone`: similarity a document needs on meaning alone. `with evidence`: what it needs when two words or a named service also match. Score = mean of exact@1 and abstain F1. Best: alone 0.64, with evidence 0.56 (score 0.769).

| alone | with evidence | exact@1 | MRR | abstain P | abstain R | abstain F1 | score |
| ---: | ---: | --- | --- | ---: | ---: | ---: | ---: |
| 0.62 | 0.54 | 0.86 [0.80, 0.93] | 0.89 [0.83, 0.94] | 0.529 | 0.6 | 0.562 | 0.712 |
| 0.62 | 0.56 | 0.86 [0.80, 0.93] | 0.89 [0.83, 0.94] | 0.579 | 0.733 | 0.647 | 0.754 |
| 0.62 | 0.58 | 0.86 [0.80, 0.93] | 0.89 [0.83, 0.94] | 0.579 | 0.733 | 0.647 | 0.754 |
| 0.62 | 0.6 | 0.86 [0.80, 0.93] | 0.89 [0.83, 0.94] | 0.579 | 0.733 | 0.647 | 0.754 |
| 0.64 | 0.54 | 0.87 [0.81, 0.93] | 0.89 [0.83, 0.94] | 0.526 | 0.667 | 0.588 | 0.73 |
| 0.64 | 0.56 | 0.87 [0.81, 0.93] | 0.89 [0.83, 0.94] | 0.571 | 0.8 | 0.666 | 0.769 |
| 0.64 | 0.58 | 0.87 [0.81, 0.93] | 0.89 [0.83, 0.94] | 0.571 | 0.8 | 0.666 | 0.769 |
| 0.64 | 0.6 | 0.87 [0.81, 0.93] | 0.89 [0.83, 0.94] | 0.571 | 0.8 | 0.666 | 0.769 |
| 0.66 | 0.54 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.5 | 0.667 | 0.572 | 0.717 |
| 0.66 | 0.56 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.545 | 0.8 | 0.648 | 0.755 |
| 0.66 | 0.58 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.545 | 0.8 | 0.648 | 0.755 |
| 0.66 | 0.6 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.545 | 0.8 | 0.648 | 0.755 |
| 0.68 | 0.54 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.5 | 0.667 | 0.572 | 0.717 |
| 0.68 | 0.56 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.545 | 0.8 | 0.648 | 0.755 |
| 0.68 | 0.58 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.545 | 0.8 | 0.648 | 0.755 |
| 0.68 | 0.6 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.545 | 0.8 | 0.648 | 0.755 |
| 0.7 | 0.54 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.5 | 0.667 | 0.572 | 0.717 |
| 0.7 | 0.56 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.545 | 0.8 | 0.648 | 0.755 |
| 0.7 | 0.58 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.545 | 0.8 | 0.648 | 0.755 |
| 0.7 | 0.6 | 0.86 [0.80, 0.93] | 0.88 [0.82, 0.94] | 0.545 | 0.8 | 0.648 | 0.755 |

## Intent: rules vs LLM vs cascade (the LLM decides below 0.7)

| Mode | n | correct | asked back | confident wrong | LLM used |
| --- | ---: | --- | ---: | --- | ---: |
| rules | 84 | 0.52 [0.42, 0.63] | 35 | 0.06 [0.01, 0.12] | 0 |
| llm | 84 | 0.70 [0.59, 0.80] | 3 | 0.26 [0.18, 0.36] | 84 |
| cascade | 84 | 0.66 [0.55, 0.75] | 23 | 0.07 [0.02, 0.13] | 37 |

## Classification: rules vs logistic regression vs LLM vs cascade

Over every labelled corpus file. A wrong label sent to review is a safe miss; a wrong label stored without review is a confident wrong.

| Origin | mode | docs | correct | to review | confident wrong | LLM used |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| brownfield | cascade | 27 | 27 | 0 | 0 | 0 |
| brownfield | llm | 27 | 27 | 0 | 0 | 24 |
| brownfield | lr | 27 | 27 | 0 | 0 | 0 |
| brownfield | lr_cascade | 27 | 27 | 0 | 0 | 0 |
| brownfield | rules | 27 | 27 | 0 | 0 | 0 |
| greenfield | cascade | 12 | 12 | 0 | 0 | 0 |
| greenfield | llm | 12 | 12 | 0 | 0 | 0 |
| greenfield | lr | 12 | 12 | 0 | 0 | 0 |
| greenfield | lr_cascade | 12 | 12 | 0 | 0 | 0 |
| greenfield | rules | 12 | 12 | 0 | 0 | 0 |
| hard | cascade | 5 | 4 | 3 | 0 | 3 |
| hard | llm | 5 | 4 | 0 | 1 | 5 |
| hard | lr | 5 | 2 | 3 | 0 | 0 |
| hard | lr_cascade | 5 | 4 | 3 | 0 | 3 |
| hard | rules | 5 | 2 | 3 | 0 | 0 |
| reference | cascade | 10 | 10 | 0 | 0 | 0 |
| reference | llm | 10 | 10 | 0 | 0 | 0 |
| reference | lr | 10 | 10 | 0 | 0 | 0 |
| reference | lr_cascade | 10 | 10 | 0 | 0 | 0 |
| reference | rules | 10 | 10 | 0 | 0 | 0 |
