-- obisflow · 016 · admin accounts: email and password, with roles
-- Run after 015. Safe to run again.
--
-- The admin site used to sign in with the one ADMIN_TOKEN. Now each person
-- has their own account. The first super admin is made on the sign-in page
-- with ADMIN_TOKEN (once, while there are no accounts); after that a super
-- admin adds the others. ADMIN_TOKEN still works as a bearer token for
-- scripts, with super admin rights.
--
-- Roles:
--   superadmin  everything, and adds, changes and removes admins
--   admin       everything except managing admins
--   finance     money: pays and rejects withdrawals; reads everything
--   support     users: verifies IDs and payment methods; reads everything
--
-- Passwords are scrypt hashes. A session is a random token kept only as its
-- SHA-256, valid for 12 hours.

create table if not exists app.admin_users (
  id             uuid        primary key default gen_random_uuid(),
  email          text        not null unique,
  name           text        not null default '',
  role           text        not null check (role in ('superadmin', 'admin', 'finance', 'support')),
  password_hash  text        not null,
  active         boolean     not null default true,
  created_by     uuid        references app.admin_users(id) on delete set null,
  created_at     timestamptz not null default now(),
  last_login_at  timestamptz
);

create table if not exists app.admin_sessions (
  token_hash  text        primary key,
  admin_id    uuid        not null references app.admin_users(id) on delete cascade,
  ip          text,
  user_agent  text,
  created_at  timestamptz not null default now(),
  expires_at  timestamptz not null
);
create index if not exists admin_sessions_admin on app.admin_sessions (admin_id);

-- who made each change on the admin site
alter table app.admin_actions add column if not exists actor text;

alter table app.admin_users    enable row level security;
alter table app.admin_sessions enable row level security;
