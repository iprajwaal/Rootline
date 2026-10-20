-- Every change to the library, written in the same transaction as the change itself.
create table audit (
    id         bigint generated always as identity primary key,
    at         timestamptz not null default now(),
    actor      text not null,
    action     text not null,
    source_key text,
    doc_ids    text[] not null default '{}',
    request_id text
);

create index audit_at on audit (at desc);
create index audit_doc_ids on audit using gin (doc_ids);
