-- =============================================================================
-- orbisflow · 005 · preferences, two-factor codes, sessions
-- Run after 004, in the Supabase SQL editor. Safe to re-run.
-- =============================================================================

-- ------------------------------------------------------------- preferences --
-- one row per user, created with defaults the first time it is read
create table if not exists app.preferences (
  user_id                  uuid primary key references app.profiles (id) on delete cascade,
  -- the ticket
  default_stake            numeric(14, 2) not null default 25 check (default_stake > 0),
  default_duration         text           not null default '5m',
  confirm_trades           boolean        not null default false,
  -- messages
  settlement_notifications boolean        not null default true,
  marketing_emails         boolean        not null default false,
  login_alerts             boolean        not null default true,
  -- security
  withdrawal_confirm       boolean        not null default true,   -- a code before every withdrawal
  twofa_enabled            boolean        not null default false,
  twofa_channel            text           check (twofa_channel in ('email', 'sms')),
  password_changed_at      timestamptz,
  -- responsible trading
  deposit_limit_daily      numeric(14, 2) check (deposit_limit_daily is null or deposit_limit_daily > 0),
  loss_limit_daily         numeric(14, 2) check (loss_limit_daily is null or loss_limit_daily > 0),
  cooling_off_until        timestamptz,
  self_excluded_until      timestamptz,
  updated_at               timestamptz    not null default now()
);

drop trigger if exists preferences_touch on app.preferences;
create trigger preferences_touch before update on app.preferences
  for each row execute function app.touch_updated_at();


-- --------------------------------------------------------------- one-time codes --
-- six-digit codes for signing in, turning two-factor on or off, and withdrawing.
-- Only a salted hash is kept; five wrong tries or ten minutes and it is dead.
create table if not exists app.otp_codes (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid        not null references app.profiles (id) on delete cascade,
  purpose      text        not null check (purpose in ('login', 'enable_2fa', 'disable_2fa', 'withdrawal')),
  channel      text        not null check (channel in ('email', 'sms')),
  code_hash    text        not null,
  salt         text        not null,
  session_id   text,                                   -- a sign-in code belongs to one session
  attempts     integer     not null default 0,
  expires_at   timestamptz not null,
  consumed_at  timestamptz,
  created_at   timestamptz not null default now()
);
create index if not exists otp_codes_user_idx on app.otp_codes (user_id, purpose, created_at desc);


-- ---------------------------------------------------------------- sessions --
-- one row per Supabase session (the session_id claim in its tokens): the
-- device it runs on, whether it has passed two-factor, and whether it was revoked
create table if not exists app.sessions (
  id              text primary key,                    -- Supabase's session_id
  user_id         uuid        not null references app.profiles (id) on delete cascade,
  device          text,
  ip              text,
  mfa_verified    boolean     not null default false,
  created_at      timestamptz not null default now(),
  last_seen_at    timestamptz not null default now(),
  revoked_at      timestamptz
);
create index if not exists sessions_user_idx on app.sessions (user_id, last_seen_at desc);


-- ----------------------------------------------------------------- locked --
alter table app.preferences enable row level security;
alter table app.otp_codes   enable row level security;
alter table app.sessions    enable row level security;
revoke all on app.preferences, app.otp_codes, app.sessions from anon, authenticated;
