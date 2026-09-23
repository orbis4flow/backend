# SQL

The backend does not create or change tables on its own. You run these files by hand in the Supabase SQL editor (Dashboard → SQL Editor → New query), paste, Run.

## Setup, in this order

| File | What it does |
|---|---|
| `001_schema.sql` | Creates the `app` schema and its tables: profiles, accounts, payment methods, transactions, webhook events. |
| `002_security.sql` | Makes sure the browser-facing roles (`anon`, `authenticated`) can never reach the `app` schema, and turns row level security on. |
| `003_trades.sql` | The trades table: every contract, its stake, prices and result. |
| `005_preferences_security.sql` | Preferences and limits, one-time codes, and signed-in devices. |
| `004_notifications_usdt_referrals.sql` | The email/SMS log, what automatic USDT deposits and referral payouts need in `transactions`, and weekly referral earnings. |
| `admin/check_setup.sql` | Confirms both ran. Every row should read `ok`. |

Both setup files are safe to run again.

A new change to the schema goes in a new numbered file (`003_…sql`), never an edit to one that has already run.

## Admin queries (`admin/`)

Run one block at a time and replace the placeholder values first.

| File | For |
|---|---|
| `lookups.sql` | A user and their balances, their transactions, stuck deposits, referral trees, the latest provider callbacks, today's totals. |
| `withdrawals.sql` | Withdrawals waiting for review: list them, approve one you have paid, or reject one and refund it. |
| `payment_methods.sql` | Bank accounts and USDT wallets waiting to be verified, and verifying or disabling a method. |

## Why a separate `app` schema

Supabase publishes the `public` schema through its Data API, which browsers can call with the publishable key. The `app` schema is not published, and `002_security.sql` revokes every grant on it from `anon` and `authenticated`. Only the backend, connected with the database password, can read or write it.
