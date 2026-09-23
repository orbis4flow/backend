-- =============================================================================
-- orbisflow · 003 · trades
-- Run after 001 and 002, in the Supabase SQL editor. Safe to re-run.
--
-- One row per contract. Placing a trade takes the stake from the account in
-- the same transaction that writes this row; settling it pays back what it
-- returned. Demo only for now: the API refuses real-account trades until
-- prices and settlement come from the server (see backend README).
-- =============================================================================

create table if not exists app.trades (
  id            uuid primary key default gen_random_uuid(),
  ref           text           not null unique,                 -- OB-XXXXXXX, shown to the user
  user_id       uuid           not null references app.profiles (id) on delete cascade,
  account_id    uuid           not null references app.accounts (id) on delete cascade,
  account_kind  text           not null check (account_kind in ('demo', 'real')),
  symbol        text           not null,
  direction     text           not null check (direction in ('Rise', 'Fall')),
  stake         numeric(14, 2) not null check (stake > 0),
  payout_pct    numeric(6, 2)  not null check (payout_pct > 0 and payout_pct <= 100),
  duration_s    integer        not null check (duration_s between 1 and 86400),
  entry_price   numeric(20, 8) not null,
  exit_price    numeric(20, 8),
  status        text           not null default 'open'
                check (status in ('open', 'won', 'lost', 'sold')),
  returned      numeric(14, 2) not null default 0,              -- what went back to the balance
  profit        numeric(14, 2),                                 -- returned - stake, once settled
  settled_by    text,                                           -- 'price', 'sold', 'expired'
  opened_at     timestamptz    not null default now(),
  expires_at    timestamptz    not null,
  settled_at    timestamptz
);

create index if not exists trades_user_idx on app.trades (user_id, opened_at desc);
create index if not exists trades_open_idx on app.trades (user_id, expires_at) where status = 'open';

-- locked down like every other table in the schema
alter table app.trades enable row level security;
revoke all on app.trades from anon, authenticated;
