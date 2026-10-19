create extension if not exists vector;
create extension if not exists pg_trgm;

create table documents (
    doc_id          text primary key,
    -- One source file maps to one document. A split mixed doc uses source_key#rca and #sop.
    source_key      text not null unique,
    doc_type        text not null check (doc_type in ('rca', 'sop', 'ref')),
    origin          text not null check (origin in ('greenfield', 'brownfield', 'reference')),
    title           text not null,
    services        text[] not null default '{}',
    status          text not null default 'active'
                    check (status in ('active', 'superseded', 'deleted')),
    current_version int,
    cluster_id      text,
    is_canonical    boolean not null default true,
    confidence      real not null,
    needs_review    boolean not null default false,
    effective_date  date,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index documents_title_trgm on documents using gin (title gin_trgm_ops);
create index documents_services on documents using gin (services);

create table document_versions (
    doc_id                text not null references documents (doc_id) on delete cascade,
    version               int not null,
    raw_sha256            text not null,
    content_hash          text not null,
    parsed                jsonb not null,
    record                jsonb not null,
    extraction_confidence real not null,
    created_at            timestamptz not null default now(),
    primary key (doc_id, version),
    unique (doc_id, content_hash)
);

create index document_versions_raw_sha256 on document_versions (raw_sha256);
create index document_versions_content_hash on document_versions (content_hash);

-- Extra source keys that point at an existing document (same file or same text under a new name).
create table aliases (
    source_key text primary key,
    doc_id     text not null references documents (doc_id) on delete cascade,
    reason     text not null check (reason in ('same_raw', 'same_content'))
);

-- RCA, SOP and reference chunks live in separate partitions, so a search can only read one type.
create table chunks (
    chunk_id    text not null,
    doc_type    text not null,
    doc_id      text not null references documents (doc_id) on delete cascade,
    version     int not null,
    section     text not null,
    ordinal     int not null,
    body        text not null,
    -- english for normal words, simple keeps error codes and IDs unstemmed.
    tsv         tsvector generated always as (
                    to_tsvector('english', body) || to_tsvector('simple', body)
                ) stored,
    embedding   vector(384) not null,
    embed_model text not null,
    primary key (doc_type, chunk_id)
) partition by list (doc_type);

create table chunks_rca partition of chunks for values in ('rca');
create table chunks_sop partition of chunks for values in ('sop');
create table chunks_ref partition of chunks for values in ('ref');

create index chunks_tsv on chunks using gin (tsv);
create index chunks_doc_id on chunks (doc_id);
create index chunks_rca_embedding on chunks_rca using hnsw (embedding vector_cosine_ops);
create index chunks_sop_embedding on chunks_sop using hnsw (embedding vector_cosine_ops);
create index chunks_ref_embedding on chunks_ref using hnsw (embedding vector_cosine_ops);

create table sop_steps (
    doc_id       text not null references documents (doc_id) on delete cascade,
    version      int not null,
    section      text not null check (section in ('steps', 'rollback')),
    ordinal      int not null,
    instruction  text not null,
    command      text,
    action       text,
    check_name   text,
    expect       text,
    source_start int,
    source_end   int,
    primary key (doc_id, version, section, ordinal)
);

-- No foreign key on to_doc: a link can name a document that is not ingested yet.
create table doc_links (
    from_doc text not null references documents (doc_id) on delete cascade,
    to_doc   text not null,
    kind     text not null check (kind in ('related', 'supersedes', 'split_from')),
    primary key (from_doc, to_doc, kind)
);

create table review_items (
    id          bigint generated always as identity primary key,
    doc_id      text references documents (doc_id) on delete cascade,
    source_key  text,
    reason      text not null,
    details     jsonb not null default '{}',
    status      text not null default 'open' check (status in ('open', 'approved', 'rejected')),
    created_at  timestamptz not null default now(),
    resolved_at timestamptz
);

create table incidents (
    id          bigint generated always as identity primary key,
    fingerprint text not null,
    service     text,
    alert_name  text,
    status      text not null default 'open' check (status in ('open', 'resolved')),
    payload     jsonb not null,
    thread_id   text,
    created_at  timestamptz not null default now(),
    resolved_at timestamptz
);

-- An alert storm repeats the same fingerprint. Keep one open incident per fingerprint.
create unique index incidents_open_fingerprint on incidents (fingerprint) where status = 'open';

create table ledger (
    id             bigint generated always as identity primary key,
    request_id     text not null,
    kind           text not null,
    task           text,
    model          text,
    prompt_version text,
    input_tokens   int not null default 0,
    output_tokens  int not null default 0,
    cost_usd       numeric(12, 6) not null default 0,
    latency_ms     int not null,
    degraded       text[] not null default '{}',
    created_at     timestamptz not null default now()
);

create index ledger_created_at on ledger (created_at);
