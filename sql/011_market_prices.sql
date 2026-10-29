-- obisflow · 011 · the generated price feed's last price per market
-- The API saves it every few seconds and carries on from it after a restart,
-- so prices never jump back to their starting level. Safe to run again.
create table if not exists app.market_prices (
  sym         text          primary key,
  price       numeric(20, 8) not null,
  updated_at  timestamptz   not null default now()
);

alter table app.market_prices enable row level security;
revoke all on app.market_prices from anon, authenticated;

