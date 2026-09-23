-- =============================================================================
-- orbisflow · 001 · schema
-- Run once in the Supabase SQL editor (Dashboard → SQL Editor → New query).
-- Safe to re-run: every statement is "if not exists" / "or replace".
--
-- Everything lives in its own schema, `app`, not `public`. Supabase exposes
-- `public` through its Data API; `app` is reached only by the backend over
-- the database connection, so no table here is reachable from a browser.
-- =============================================================================

create schema if not exists app;

-- keeps updated_at honest on every table that has one
create or replace function app.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;


-- ------------------------------------------------------------------ profiles --
-- one row per Supabase Auth user, created by the backend on first sign-in
create table if not exists app.profiles (
  id             uuid primary key references auth.users (id) on delete cascade,
  email          text        not null,
  full_name      text,
  phone          text,                                   -- E.164, e.g. +254712345678
  country_code   char(2),                                -- ISO 3166-1 alpha-2, lower case
  country_name   text,
  currency       text        not null default 'USD'
                 check (currency in ('USD', 'KES', 'USDT')),
  referral_code  text        not null unique,            -- e.g. ORBIS-7KQ2
  referred_by    uuid        references app.profiles (id) on delete set null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  constraint profiles_not_self_referred check (referred_by is null or referred_by <> id)
);

create index if not exists profiles_referred_by_idx on app.profiles (referred_by);
create index if not exists profiles_email_idx on app.profiles (lower(email));

drop trigger if exists profiles_touch on app.profiles;
create trigger profiles_touch before update on app.profiles
  for each row execute function app.touch_updated_at();


-- ------------------------------------------------------------------ accounts --
-- every user has a demo and a real account; balances never go below zero
create table if not exists app.accounts (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid           not null references app.profiles (id) on delete cascade,
  kind        text           not null check (kind in ('demo', 'real')),
  currency    text           not null default 'USD',
  balance     numeric(14, 2) not null default 0 check (balance >= 0),
  created_at  timestamptz    not null default now(),
  updated_at  timestamptz    not null default now(),
  unique (user_id, kind)
);

drop trigger if exists accounts_touch on app.accounts;
create trigger accounts_touch before update on app.accounts
  for each row execute function app.touch_updated_at();


-- ----------------------------------------------------------- payment methods --
-- where money comes from and goes to. `fingerprint` is what makes two entries
-- the same method (a phone number, an account number, a card signature).
-- A user removing a method sets removed_at (it can be added back); an admin
-- blocking one sets status 'disabled' (it cannot).
create table if not exists app.payment_methods (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid        not null references app.profiles (id) on delete cascade,
  kind         text        not null check (kind in ('mpesa', 'card', 'bank', 'usdt')),
  label        text        not null,                   -- "M-Pesa", "Visa", "Equity Bank"
  masked       text        not null,                   -- "+254 7•• ••• 412", "••••4417"
  fingerprint  text        not null,
  details      jsonb       not null default '{}'::jsonb,
  status       text        not null default 'pending'
               check (status in ('pending', 'verified', 'disabled')),
  is_default   boolean     not null default false,
  removed_at   timestamptz,                            -- set when the user removes it
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (user_id, kind, fingerprint)
);

create index if not exists payment_methods_user_idx on app.payment_methods (user_id);
-- at most one default method per user
create unique index if not exists payment_methods_one_default
  on app.payment_methods (user_id) where is_default;

drop trigger if exists payment_methods_touch on app.payment_methods;
create trigger payment_methods_touch before update on app.payment_methods
  for each row execute function app.touch_updated_at();


-- -------------------------------------------------------------- transactions --
-- deposits and withdrawals. `reference` is ours and is what providers echo
-- back; `provider_ref` is theirs (PayHero reference, Paystack reference).
create table if not exists app.transactions (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid           not null references app.profiles (id) on delete cascade,
  account_id         uuid           not null references app.accounts (id) on delete cascade,
  payment_method_id  uuid           references app.payment_methods (id) on delete set null,
  type               text           not null check (type in ('deposit', 'withdrawal')),
  method             text           not null check (method in ('mpesa', 'card', 'bank', 'usdt')),
  provider           text           not null check (provider in ('payhero', 'paystack', 'manual')),
  status             text           not null default 'pending'
                     check (status in ('pending', 'processing', 'review', 'completed', 'failed', 'cancelled')),
  amount_usd         numeric(14, 2) not null check (amount_usd > 0),  -- what the user asked for
  fee_usd            numeric(14, 2) not null default 0,
  net_usd            numeric(14, 2) not null,       -- deposit: credited · withdrawal: debited
  local_amount       numeric(14, 2),                -- e.g. the KSh sent or received
  local_currency     text,
  fx_rate            numeric(12, 4),
  reference          text           not null unique,
  provider_ref       text,
  provider_receipt   text,                          -- e.g. the M-Pesa receipt number
  destination        text,                          -- masked, for display
  failure_reason     text,
  meta               jsonb          not null default '{}'::jsonb,
  created_at         timestamptz    not null default now(),
  updated_at         timestamptz    not null default now(),
  completed_at       timestamptz
);

create index if not exists transactions_user_idx on app.transactions (user_id, created_at desc);
create index if not exists transactions_provider_ref_idx on app.transactions (provider_ref);
create index if not exists transactions_open_idx on app.transactions (status)
  where status in ('pending', 'processing', 'review');

drop trigger if exists transactions_touch on app.transactions;
create trigger transactions_touch before update on app.transactions
  for each row execute function app.touch_updated_at();


-- ------------------------------------------------------------ webhook events --
-- every provider callback, kept as received, for audits and replays
create table if not exists app.webhook_events (
  id           bigserial primary key,
  provider     text        not null,
  reference    text,
  payload      jsonb       not null,
  received_at  timestamptz not null default now(),
  processed    boolean     not null default false,
  error        text
);

create index if not exists webhook_events_ref_idx on app.webhook_events (provider, reference);
