-- Vectors now come from Vertex text-embedding-005 with 768 numbers. The old 384-number vectors
-- came from another model and cannot be compared with them, so they are dropped. A chunk with
-- no vector is embedded again the next time its file is read, which is also how a chunk
-- stored while the model was unreachable gets its vector later.
drop index chunks_rca_embedding;
drop index chunks_sop_embedding;
drop index chunks_ref_embedding;
alter table chunks drop column embedding;
alter table chunks add column embedding vector(768);
create index chunks_rca_embedding on chunks_rca using hnsw (embedding vector_cosine_ops);
create index chunks_sop_embedding on chunks_sop using hnsw (embedding vector_cosine_ops);
create index chunks_ref_embedding on chunks_ref using hnsw (embedding vector_cosine_ops);
