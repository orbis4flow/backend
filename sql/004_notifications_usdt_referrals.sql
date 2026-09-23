-- =============================================================================
-- orbisflow · 004 · notifications, automatic USDT deposits, referral earnings
-- Run after 003, in the Supabase SQL editor. Safe to re-run.
-- =============================================================================

-- ------------------------------------------------------------ notifications --
-- every email and SMS the API sends (or tried to), for support and audits
create table if not exists app.notifications (
  id           bigserial primary key,
  user_id      uuid references app.profiles (id) on delete set null,
  channel      text        not null check (channel in ('email', 'sms')),
  kind         text        not null,                     -- welcome, deposit_received, …
  to_address   text        not null,
  subject      text,
  status       text        not null check (status in ('sent', 'failed', 'skipped')),
  provider_id  text,
  error        text,
  created_at   timestamptz not null default now()
);
create index if not exists notifications_user_idx on app.notifications (user_id, created_at desc);


-- ---------------------------------------------- transactions: new sources --
-- USDT deposits are matched on the TRON chain ('tron'); referral payouts
-- land in the real balance as their own kind of transaction.
alter table app.transactions drop constraint if exists transactions_provider_check;
alter table app.transactions add constraint transactions_provider_check
  check (provider in ('payhero', 'paystack', 'manual', 'tron', 'orbisflow'));

alter table app.transactions drop constraint if exists transactions_type_check;
alter table app.transactions add constraint transactions_type_check
  check (type in ('deposit', 'withdrawal', 'referral'));

alter table app.transactions drop constraint if exists transactions_method_check;
alter table app.transactions add constraint transactions_method_check
  check (method in ('mpesa', 'card', 'bank', 'usdt', 'referral'));

-- one on-chain transfer can only ever credit one deposit
create unique index if not exists transactions_tron_tx_uniq
  on app.transactions (provider_ref) where provider = 'tron' and provider_ref is not null;

-- an open USDT request is told apart by its exact amount, so no two open
-- requests may ask for the same one
create unique index if not exists transactions_tron_open_amount_uniq
  on app.transactions (local_amount) where provider = 'tron' and status = 'pending';


-- ------------------------------------------------------- referral earnings --
-- one row per referrer per week (Monday to Sunday, Nairobi time). Pending
-- while the week runs and until it is paid on the Thursday after it closes.
create table if not exists app.referral_earnings (
  id             uuid primary key default gen_random_uuid(),
  referrer_id    uuid           not null references app.profiles (id) on delete cascade,
  week_start     date           not null,
  active         integer        not null default 0,   -- referrals who traded real money that week
  volume         numeric(14, 2) not null default 0,   -- their real-money stakes
  spread         numeric(14, 2) not null default 0,   -- what orbisflow earned on it
  tier_pct       numeric(5, 2)  not null default 0,   -- the referrer's share, 20 / 28 / 35
  amount         numeric(14, 2) not null default 0,   -- the referrer's earnings
  status         text           not null default 'pending' check (status in ('pending', 'paid')),
  transaction_id uuid           references app.transactions (id) on delete set null,
  updated_at     timestamptz    not null default now(),
  paid_at        timestamptz,
  unique (referrer_id, week_start)
);
create index if not exists referral_earnings_referrer_idx on app.referral_earnings (referrer_id, week_start desc);


-- ----------------------------------------------------------------- locked --
alter table app.notifications     enable row level security;
alter table app.referral_earnings enable row level security;
revoke all on app.notifications, app.referral_earnings from anon, authenticated;
revoke all on sequence app.notifications_id_seq from anon, authenticated;
