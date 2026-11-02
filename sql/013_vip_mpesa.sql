-- obisflow · 013 · the VIP M-Pesa demo rail (as on Nexas)
-- Run after 012. Safe to run again.
--
-- A VIP's M-Pesa deposits and withdrawals settle against a demo wallet that
-- the companion M-Pesa clone app shows, instead of PayHero or Kora: money
-- comes off the handset onto the real trading balance at once, and a
-- withdrawal lands back on the handset at once. No real money moves and no
-- provider key is touched. Standard accounts never reach any of this.
--
-- The PIN is stored in plain text on purpose: an admin assigns it (never the
-- customer), so it is nobody's real M-Pesa PIN, and the admin site reads it
-- back to tell the tester what to type. It guards a prop balance only.
--
-- Amounts here are KES minor units (cents): 100000 = Ksh 1,000.00.

create table if not exists app.mpesa_demo_wallet (
  user_id            uuid        primary key references app.profiles (id) on delete cascade,
  pin                text        not null check (pin ~ '^\d{4}$'),
  device_token       uuid        not null default gen_random_uuid(),
  balance_minor      bigint      not null default 0 check (balance_minor >= 0),
  -- Fuliza: an overdraft set per wallet by the admin; used is what is owed,
  -- repaid first from the next money in
  fuliza_limit_minor bigint      not null default 0 check (fuliza_limit_minor >= 0),
  fuliza_used_minor  bigint      not null default 0 check (fuliza_used_minor >= 0),
  holder_name        text,
  phone              text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

-- the handset presents only a PIN, so a PIN names exactly one wallet
create unique index if not exists mpesa_demo_wallet_pin_key   on app.mpesa_demo_wallet (pin);
create unique index if not exists mpesa_demo_wallet_token_key on app.mpesa_demo_wallet (device_token);

create table if not exists app.mpesa_demo_tx (
  id                  uuid        primary key default gen_random_uuid(),
  user_id             uuid        not null references app.profiles (id) on delete cascade,
  kind                text        not null check (kind in
                        ('DEPOSIT', 'WITHDRAWAL', 'AGENT_WITHDRAWAL', 'RECEIVE', 'FULIZA_REPAY', 'REVERSAL')),
  title               text        not null,
  subtitle            text        not null default '',
  amount_minor        bigint      not null,          -- signed: negative left the phone
  balance_after_minor bigint      not null,
  fuliza_after_minor  bigint      not null default 0,
  reference           text        not null,
  created_at          timestamptz not null default now()
);

create index if not exists mpesa_demo_tx_user_created_idx on app.mpesa_demo_tx (user_id, created_at desc);

alter table app.mpesa_demo_wallet enable row level security;
alter table app.mpesa_demo_tx     enable row level security;
revoke all on app.mpesa_demo_wallet, app.mpesa_demo_tx from anon, authenticated;

-- a VIP deposit or withdrawal is still a row in app.transactions, settled like
-- any other, so it shows in the cashier, the admin site and the ledger; the
-- provider says plainly it was not real money
alter table app.transactions drop constraint if exists transactions_provider_check;
alter table app.transactions add constraint transactions_provider_check
  check (provider in ('kora', 'payhero', 'paystack', 'manual', 'tron', 'orbisflow', 'admin', 'mpesa_demo'));
