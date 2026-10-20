-- One row per chat turn, so /ops can show latency and cost for each kind of request. Ledger
-- rows carry the same request_id, which joins a turn to the LLM and embedding calls it made.
create table requests (
    request_id text primary key,
    kind       text not null,
    path       text not null,
    ms         int not null,
    llm_calls  int not null,
    at         timestamptz not null default now()
);

create index requests_at on requests (at desc);
