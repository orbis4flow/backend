-- obisflow · 017 · marketers and their live sessions
-- Run after 016. Safe to run again.
--
-- A marketer is an admin account with the role 'marketer'. They go live (a
-- TikTok, Instagram or YouTube stream, say) from the admin site, and the real
-- money deposited while the session runs is counted as theirs. They see only
-- their own sessions and the money that came in during them: no users, no
-- other pages.
--
-- Only one session runs at a time, across all marketers, so every deposit
-- belongs to at most one session and nobody's figures overlap.

alter table app.admin_users drop constraint if exists admin_users_role_check;
alter table app.admin_users add constraint admin_users_role_check
  check (role in ('superadmin', 'admin', 'finance', 'support', 'marketer'));

create table if not exists app.live_sessions (
  id            uuid        primary key default gen_random_uuid(),
  marketer_id   uuid        not null references app.admin_users(id) on delete cascade,
  channel       text        not null default '',     -- TikTok, Instagram, YouTube...
  note          text,
  started_at    timestamptz not null default now(),
  ended_at      timestamptz,
  started_by    uuid        references app.admin_users(id) on delete set null,
  ended_by      uuid        references app.admin_users(id) on delete set null
);
create index if not exists live_sessions_marketer on app.live_sessions (marketer_id, started_at desc);
-- one live session at a time, everywhere
create unique index if not exists live_sessions_one_live on app.live_sessions ((true)) where ended_at is null;

alter table app.live_sessions enable row level security;
