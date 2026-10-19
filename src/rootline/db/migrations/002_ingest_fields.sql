alter table documents add column alerts text[] not null default '{}';
-- False when an extraction did not pass the source grounding check. Its steps are not shown.
alter table documents add column verified boolean not null default true;

alter table document_versions add column facts jsonb not null default '{}';
alter table document_versions add column classification jsonb not null default '{}';
alter table document_versions add column extraction_method text;

create index documents_cluster on documents (cluster_id);
create index documents_status_type on documents (status, doc_type);

-- Lets a re-upload of a file that is already an alias be seen as unchanged.
alter table aliases add column raw_sha256 text;
