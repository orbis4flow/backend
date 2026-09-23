-- =============================================================================
-- orbisflow · 002 · lock the app schema down
-- Run after 001. Safe to re-run.
--
-- The backend connects as `postgres` (the table owner), which is unaffected.
-- These statements make sure the browser-facing roles Supabase uses
-- (`anon`, `authenticated`) can never read or write this data, even if the
-- `app` schema is later added to the Data API by mistake.
-- =============================================================================

revoke all on schema app from anon, authenticated;
revoke all on all tables    in schema app from anon, authenticated;
revoke all on all sequences in schema app from anon, authenticated;
revoke all on all functions in schema app from anon, authenticated;

alter default privileges in schema app revoke all on tables    from anon, authenticated;
alter default privileges in schema app revoke all on sequences from anon, authenticated;
alter default privileges in schema app revoke all on functions from anon, authenticated;

-- row level security on, with no policies: deny everything to everyone but the owner
alter table app.profiles        enable row level security;
alter table app.accounts        enable row level security;
alter table app.payment_methods enable row level security;
alter table app.transactions    enable row level security;
alter table app.webhook_events  enable row level security;
