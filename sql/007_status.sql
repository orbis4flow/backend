-- One row per service per check (every five minutes), for the status page.
-- The backend keeps about 120 days. Safe to run again.
create table if not exists app.status_checks (
  id          bigserial   primary key,
  service     text        not null,
  ok          boolean     not null,
  latency_ms  integer,
  checked_at  timestamptz not null default now()
);
create index if not exists status_checks_time_idx on app.status_checks (checked_at desc, service);

alter table app.status_checks enable row level security;
revoke all on app.status_checks from anon, authenticated;
