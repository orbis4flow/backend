-- obisflow · 009 · admin actions
-- Every change made from the admin site, with what it was done to and why.
-- Safe to run again.
create table if not exists app.admin_actions (
  id          bigserial   primary key,
  action      text        not null,          -- e.g. withdrawal.approve, method.block
  target      text        not null,          -- a transaction reference or a payment method id
  user_id     uuid        references app.profiles (id) on delete set null,
  note        text,
  ip          text,
  created_at  timestamptz not null default now()
);
create index if not exists admin_actions_time_idx on app.admin_actions (created_at desc);

alter table app.admin_actions enable row level security;
revoke all on app.admin_actions from anon, authenticated;
