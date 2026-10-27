-- obisflow · 010 · event log for the admin site's Logs page
-- M-Pesa prompts, card checkouts, payment results, provider callbacks,
-- sign-ups, Google sign-ins, new accounts, sign-ins and price-feed drops.
-- The API keeps 90 days. Safe to run again.
create table if not exists app.events (
  id          bigserial   primary key,
  kind        text        not null,            -- e.g. mpesa.stk, card.payment, auth.google
  ok          boolean     not null,
  provider    text,                            -- kora, payhero, paystack, google, email, binance…
  ref         text,                            -- a transaction reference, where there is one
  user_id     uuid        references app.profiles (id) on delete set null,
  detail      text,                            -- why it failed, or what happened
  ms          integer,                         -- how long the provider took
  created_at  timestamptz not null default now()
);
create index if not exists events_time_idx on app.events (created_at desc);
create index if not exists events_kind_idx on app.events (kind, created_at desc);

alter table app.events enable row level security;
revoke all on app.events from anon, authenticated;
