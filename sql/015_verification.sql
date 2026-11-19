-- obisflow · 015 · identity verification before withdrawals
-- Run after 014. Safe to run again.
--
-- A user sends a proof of identity (and, if they like, a proof of address)
-- from the Verification page. Someone on the admin site looks at the files
-- and approves or rejects the submission. Withdrawals stay closed until a
-- proof of identity is approved. VIP accounts are demonstration accounts and
-- are not asked.
--
-- The files are kept here, in the database, as bytes: the browser shrinks a
-- photo to about 1600 px before it is sent, so a submission is a few hundred
-- KB. Only the admin API reads them back; the user API never returns them.

create table if not exists app.verifications (
  id           uuid        primary key default gen_random_uuid(),
  user_id      uuid        not null references app.profiles(id) on delete cascade,
  kind         text        not null check (kind in ('identity', 'address')),
  doc_type     text        not null,          -- id, passport, licence, utility, bank
  status       text        not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  reason       text,                          -- why it was rejected, shown to the user
  created_at   timestamptz not null default now(),
  decided_at   timestamptz
);
create index if not exists verifications_user on app.verifications (user_id, kind, created_at desc);
create index if not exists verifications_status on app.verifications (status, created_at);

create table if not exists app.verification_files (
  id              uuid        primary key default gen_random_uuid(),
  verification_id uuid        not null references app.verifications(id) on delete cascade,
  side            text        not null,       -- Front, Back, Photo page, Document
  mime            text        not null,
  size            integer     not null,
  data            bytea       not null,
  created_at      timestamptz not null default now()
);
create index if not exists verification_files_parent on app.verification_files (verification_id);

-- only the API touches these, with its own connection; nothing through Supabase's REST
alter table app.verifications enable row level security;
alter table app.verification_files enable row level security;
