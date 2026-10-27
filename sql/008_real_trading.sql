-- obisflow · 008 · real-money trading on server prices
-- A contract now records whether its prices came from the server's feed or
-- (demo only, on a market with no feed) from the client's chart. One the
-- server cannot price, because its feed dropped while it was open, is
-- refunded: status 'void'. Safe to run again.
alter table app.trades add column if not exists price_source text not null default 'client'
  check (price_source in ('server', 'client'));

alter table app.trades drop constraint if exists trades_status_check;
alter table app.trades add constraint trades_status_check
  check (status in ('open', 'won', 'lost', 'sold', 'void'));
