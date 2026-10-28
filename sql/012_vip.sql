-- obisflow · 012 · VIP accounts
-- A VIP is a demonstration account: only the admin site can make one, and
-- only the admin site can put money in its real balance (a 'credit') or take
-- it back (a 'debit'). That money is not real, so a VIP can neither deposit
-- nor withdraw, its trades earn no referral commission, and the admin totals
-- leave it out. Safe to run again.
alter table app.profiles add column if not exists is_vip    boolean     not null default false;
alter table app.profiles add column if not exists vip_since timestamptz;

alter table app.transactions drop constraint if exists transactions_type_check;
alter table app.transactions add constraint transactions_type_check
  check (type in ('deposit', 'withdrawal', 'referral', 'credit', 'debit'));

alter table app.transactions drop constraint if exists transactions_method_check;
alter table app.transactions add constraint transactions_method_check
  check (method in ('mpesa', 'card', 'bank', 'usdt', 'referral', 'admin'));

alter table app.transactions drop constraint if exists transactions_provider_check;
alter table app.transactions add constraint transactions_provider_check
  check (provider in ('kora', 'payhero', 'paystack', 'manual', 'tron', 'orbisflow', 'admin'));

create index if not exists profiles_vip_idx on app.profiles (id) where is_vip;
