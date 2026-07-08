-- /root/morning_brief_v2/pult/schema.sql
-- Run in Supabase SQL editor (or via psql if DATABASE_URL is available).
-- Schema: morning_brief_v2 (NOT public).

set search_path = morning_brief_v2, public;

-- Table: jobs (queue for pult)
create table if not exists jobs (
  id           bigint generated always as identity primary key,
  script       text not null,
  status       text not null default 'pending'
                 check (status in ('pending','running','done','failed','orphaned')),
  payload      jsonb not null default '{}'::jsonb,
  error        text,
  triggered_at timestamptz not null default now(),
  finished_at  timestamptz
);

-- Indexes
create index if not exists jobs_status_idx on jobs (status);
create index if not exists jobs_triggered_idx on jobs (triggered_at desc);

-- Partial unique index: prevent double-pending/running for same script+date
-- (belt + suspenders with UI block 3s)
create unique index if not exists jobs_dedup_idx
  on jobs (script, (payload->>'date'))
  where status in ('pending', 'running');

-- RPC: claim_next_job (atomic, supports parallel workers via SKIP LOCKED)
create or replace function claim_next_job() returns jobs
  language plpgsql security definer as $$
declare j jobs;
begin
  select * into j from jobs
    where status = 'pending'
    order by triggered_at asc
    for update skip locked
    limit 1;
  return j;
end $$;

-- RPC: reap_stuck_jobs (mark running > 15 min as orphaned)
create or replace function reap_stuck_jobs() returns integer
  language plpgsql as $$
declare n integer;
begin
  update jobs set status = 'orphaned',
    error = 'reaper: running > 15 min', finished_at = now()
    where status = 'running' and triggered_at < now() - interval '15 minutes';
  get diagnostics n = row_count;
  return n;
end $$;

-- Grants for Vercel Function (uses service_role key to INSERT jobs)
-- Critical: BOTH `service_role` AND `authenticator` need grants.
-- PostgREST connects as `authenticator` (login role) and sets
-- current_user via SET LOCAL ROLE based on JWT claim.
-- Postgres checks grants for the LOGIN role (authenticator),
-- so without this grant service_role JWT gets 42501.
grant insert, update, delete, select on morning_brief_v2.jobs to authenticator, service_role;
grant usage, select on sequence morning_brief_v2.jobs_id_seq to authenticator, service_role;

-- === Re-notify PostgREST to refresh schema cache ===
notify pgrst, 'reload schema';