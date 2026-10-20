# 00: Flow

The whole system first, then one diagram per part. Each section links to the file that explains it
in detail.

---

## 1. End to end

```mermaid
flowchart LR
  subgraph IN["Entry points"]
    ENG["Engineer<br/>chat UI :3000"]
    ALERT["Alert webhook<br/>POST /incidents"]
    OWNER["Doc owner<br/>POST /documents · rootline ingest"]
  end

  subgraph API["REST API :8000 (FastAPI)"]
    SEARCH["/search"]
    SUGGEST["/suggest"]
    DOCS["/documents · /docs/{id}"]
    PAGES["/review · /ops"]
    INC["/incidents"]
  end

  subgraph AGENT["Agent server :2024 (Aegra + LangGraph)"]
    GUARD["guard"] --> UND["understand"] --> ROUTE{"route"}
    ROUTE --> FIND["find"] & SUG["suggest"] & SOP["ask_sop"] & RCA["ask_rca"]
    SUG -->|nothing relevant| TS["troubleshoot agent"]
    ROUTE --> RUN["run_step"] & DRAFT["draft"]
  end

  subgraph DB["Postgres 16 + pgvector"]
    DOCSDB[("documents · versions")]
    CHUNKS[("chunks: rca · sop · ref")]
    STEPS[("sop_steps")]
    REV[("review_items · ledger · audit · requests")]
    CK[("chat checkpoints")]
  end

  subgraph MODELS["Models (Vertex AI)"]
    LLM["Gemini Flash-Lite"]
    EMB["text-embedding-005"]
  end

  SHOP["ShopLite :8080 or kind cluster"]

  ENG --> GUARD
  ALERT --> INC --> SUGGEST
  INC -->|open chat| GUARD
  OWNER --> DOCS --> DOCSDB
  FIND & SUG & SOP & RCA -.-> CHUNKS & STEPS
  UND -.->|unsure only| LLM
  RCA & TS & DRAFT -.-> LLM
  CHUNKS -.-> EMB
  TS -->|read-only checks| SHOP
  RUN -->|after approval| SHOP
  DRAFT --> REV --> PAGES
  AGENT -.-> CK
```

Two doors lead to the same retrieval code: the REST API (fast, no agent, used by the eval) and the
chat graph (memory, citations, approvals). See [01-architecture.md](01-architecture.md).

---

## 2. Ingestion: one file, one transaction

```mermaid
flowchart TD
  F["file bytes + source_key"] --> U{"same raw sha256<br/>as last time?"}
  U -->|yes| NOOP["unchanged: nothing paid for"]
  U -->|no| P["parse: md · txt · html · eml · Slack json · pdf · docx"]
  P --> R["redact secrets: own rules, then detect-secrets"]
  R --> C["classify: template → rules → logistic regression<br/>→ LLM only between 0.3 and 0.7"]
  C -->|too little text| RO["review only, not searchable"]
  C --> S{"mixed RCA + SOP?"}
  S -->|yes| SPLIT["split into #rca and #sop parts"]
  S -->|no| X
  SPLIT --> X["extract: template rules, or LLM + grounding check"]
  X --> CH["chunk by section, never inside a step · embed"]
  CH --> T["one transaction, advisory locks on source, raw and content"]
  T --> A{"same content<br/>under another name?"}
  A -->|yes| ALIAS["alias of the existing document"]
  A -->|no| W["write version n+1 · swap chunks and steps · cluster near copies"]
  W --> RV["review items: low confidence · unverified steps · secrets · possible duplicate"]
```

Details and thresholds: [03-ingestion.md](03-ingestion.md).

---

## 3. Search and suggestions

```mermaid
flowchart TD
  Q["query"] --> ID{"document ID?"}
  ID -->|yes| EXACT["that document, nothing else"]
  ID -->|no| TI{"title match ≥ 0.6?"}
  TI -->|yes| TITLE["that document · close titles shown as ambiguous"]
  TI -->|no| H["per list (rca, sop): keyword top 30 + vector top 30"]
  H --> RRF["reciprocal rank fusion"]
  RRF --> J["judge: similarity ≥ 0.64,<br/>or ≥ 0.58 with two words or a named service"]
  J --> COL["collapse brownfield copies into the canonical document"]
  COL --> OUT["RCA list and SOP list, kept apart"]

  I["issue text / alert / log"] --> CLEAN["strip noise: timestamps, IDs, pod hashes"]
  CLEAN --> FACTS["alerts · error signatures · services"]
  FACTS --> H
  OUT --> WHY["rank by shared alert ×1.3, shared error ×1.15<br/>new named error → nothing relevant"]
  WHY --> SOUT["suggestions, each with a why"]
```

Details: [04-retrieval.md](04-retrieval.md).

---

## 4. The chat graph

```mermaid
flowchart TD
  START((Start)) --> guard --> understand
  understand --> route{route}
  route --> find & ask_sop & ask_rca & run_step & draft & decline & reply
  route --> suggest
  route --> recheck
  route --> troubleshoot
  suggest -->|documents found| record
  suggest -->|nothing relevant| troubleshoot
  recheck -->|still failing| troubleshoot
  recheck -->|healthy| record
  troubleshoot --> think
  think -->|tool calls| tools --> think
  think -->|done or 6 calls| conclude
  find & ask_sop & ask_rca & run_step & draft & decline & conclude & reply --> record
  record --> END((End))
```

Every node writes a step to the trace, which the UI shows as the step log, and `record` writes one
row per turn to `requests`. Details: [05-chat-agent.md](05-chat-agent.md).

---

## 5. One incident, start to finish

```mermaid
sequenceDiagram
  participant Shop as Shop (ShopLite or kind)
  participant Alerter
  participant API as Rootline API
  participant Agent as Chat graph
  participant Eng as Engineer
  participant Rev as Reviewer

  Shop->>Alerter: health degraded (on kind, held for 2 probes)
  Alerter->>API: POST /incidents (Alertmanager format, real error line)
  API->>API: suggest: RCA list + SOP list
  Eng->>API: open the alert in the UI
  API->>Agent: thread with the alert as first message
  alt a document covers it
    Agent-->>Eng: SOP steps + RCA, cited, 0 LLM calls
  else nothing covers it
    Agent->>Shop: read-only checks, dry run of a fix
    Agent-->>Eng: numbered steps, each tested first
  end
  Eng->>Agent: run step 2
  Agent-->>Eng: approval card (command, dry-run result)
  Eng->>Agent: approve
  Agent->>Shop: run the stored command (fixing account)
  Agent-->>Eng: output, health ok, "Should I write this up?"
  Eng->>Agent: yes
  Agent->>API: SOP + RCA drafts into the review queue
  Rev->>API: approve on /review
  API->>API: ingest: searchable from now on
```

The learning loop is in [06-learning-loop.md](06-learning-loop.md); the shop, faults and alerter in
[07-shop-and-cluster.md](07-shop-and-cluster.md).
