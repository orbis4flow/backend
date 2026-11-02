-- obisflow · 014 · staged results for VIP accounts, always labelled
-- Run after 013. Safe to run again.
--
-- A switch on the admin site, for VIP accounts only. With it on, that
-- account's contracts are settled in its favour most of the time (STAGED_WIN_PCT,
-- default 90) instead of by the price at expiry, so a presentation does not
-- fall apart on a losing streak. A VIP's money is a prop (the M-Pesa demo
-- wallet, sql/013), so no real money is won.
--
-- It is never hidden: while it is on, the account's own screens carry a DEMO
-- tag, and every contract settled this way is marked staged in its history and
-- on the admin site, so nobody watching takes these results for the real odds.

alter table app.profiles add column if not exists staged_results boolean not null default false;
alter table app.trades   add column if not exists staged         boolean not null default false;

-- only a VIP may have it on, however the flag is reached
alter table app.profiles drop constraint if exists profiles_staged_needs_vip;
alter table app.profiles add constraint profiles_staged_needs_vip check (not staged_results or is_vip);
