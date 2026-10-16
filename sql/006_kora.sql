-- M-Pesa deposits now go through Kora by default (MPESA_PROVIDER=kora),
-- so transactions may name it as their provider. Safe to run again.
alter table app.transactions drop constraint if exists transactions_provider_check;
alter table app.transactions add constraint transactions_provider_check
  check (provider in ('kora', 'payhero', 'paystack', 'manual', 'tron', 'orbisflow'));
