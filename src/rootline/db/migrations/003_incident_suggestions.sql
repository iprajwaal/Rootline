-- Suggestions are kept with the incident, so its chat thread opens on the same list.
alter table incidents add column suggestions jsonb;
-- An alert storm bumps the count on the open incident instead of opening new ones.
alter table incidents add column fire_count int not null default 1;
alter table incidents alter column thread_id set default gen_random_uuid()::text;
