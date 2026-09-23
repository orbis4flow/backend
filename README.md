# orbisflow API

FastAPI service behind the orbisflow UI. It handles sign-up and sign-in (through Supabase Auth), profiles, payment methods, referral links, M-Pesa deposits and payouts (PayHero), and card deposits (Paystack). Data lives in Supabase Postgres.

This folder is its own repository. The UI repository ignores it (`.gitignore`, `.vercelignore`).

## 1 · Database (Supabase)

In the Supabase SQL editor, run in order, then check:

1. `sql/001_schema.sql`
2. `sql/002_security.sql`
3. `sql/admin/check_setup.sql`: every row should read `ok`

See `sql/README.md` for the admin queries (withdrawal review, lookups, verifying methods).

## 2 · Supabase Auth settings

Dashboard → Authentication:

- **URL Configuration** → Site URL: your UI address, e.g. `https://orbisflow.com`. Add to Redirect URLs: `https://orbisflow.com/login` (and `http://localhost:5500/login` for local work).
- **Sign In / Providers → Email**: choose whether new accounts must confirm their email. Both work: with confirmation on, sign-up answers `confirm_email: true` and the UI asks the user to check their inbox.
- **Sign In / Providers → Google** (for "Continue with Google"): turn it on and paste the Google OAuth client id and secret.
- **JWT Keys**: if the project still uses the legacy JWT secret, copy it into `SUPABASE_JWT_SECRET`. On signing keys (the default for new projects) leave it empty; tokens are checked against the project's public keys.

## 3 · Deploy on Render

1. Push this folder to its own GitHub repository.
2. Render → **New → Blueprint** → choose the repository. `render.yaml` creates the web service.
3. Fill in the values Render asks for:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Supabase → **Connect → Session pooler** string, with your password. **Not** the direct `db.mbazpiignlrayzbwbxth.supabase.co` one: that host is IPv6 only and Render cannot reach it. |
| `FRONTEND_URL` | The UI's address, e.g. `https://orbisflow.com` |
| `PUBLIC_API_URL` | This service's address, e.g. `https://orbisflow-api.onrender.com` |
| `CORS_ORIGINS` | Every address the UI is served from, comma separated, e.g. `https://orbisflow.com,https://www.orbisflow.com,https://orbis.vercel.app` |
| `PAYHERO_*`, `PAYSTACK_SECRET_KEY` | See below. Leave empty to deploy first; those payments answer "not switched on yet" until set. |

4. Open `https://<your-service>.onrender.com/health`. It should show `"database": "ok"`.
5. In the UI repository, set `API_BASE` at the top of `assets/js/api.js` to this address.

The blueprint uses the **starter** plan on purpose: a free service sleeps after 15 minutes, and a sleeping API misses payment callbacks.

## 4 · PayHero (M-Pesa)

Dashboard → API Keys: create a key, and put its username and password in `PAYHERO_API_USERNAME` / `PAYHERO_API_PASSWORD`. Dashboard → Payment Channels: the id of the till or paybill that receives payments goes in `PAYHERO_CHANNEL_ID`; the channel payouts are sent from goes in `PAYHERO_WITHDRAW_CHANNEL_ID` (it falls back to the first if empty). Keep the PayHero wallet funded, since M-Pesa payouts are paid from it.

Nothing to set for callbacks: every request carries `PUBLIC_API_URL/webhooks/payhero?token=PAYHERO_CALLBACK_TOKEN`. Render generates the token.

PayHero does not sign callbacks, so the API never credits on a callback's word: it re-reads the payment from PayHero's `/transaction-status` first.

**Check before going live:** the STK push and status calls follow PayHero's documented v2 API. The payout call (`POST /withdraw`, `payment_service: b2c`, `network_code: 63902`) follows their "withdraw to mobile" docs; confirm the field names against your dashboard's API docs, and send one small real payout, before turning on `PAYHERO_AUTO_WITHDRAW`. With it `false`, M-Pesa withdrawals wait in review (`sql/admin/withdrawals.sql`).

## 5 · Paystack (cards)

Dashboard → Settings → API Keys & Webhooks:

- Secret key → `PAYSTACK_SECRET_KEY` (`sk_test_…` to test, `sk_live_…` to go live).
- Webhook URL → `https://<your-service>.onrender.com/webhooks/paystack`.

`PAYSTACK_CURRENCY` is the currency your Paystack account settles in (`KES` for a Kenyan business). Dollar amounts are converted at `USD_KES_RATE`. Cards are charged on Paystack's own page; the card number never reaches orbisflow. A card that pays successfully is saved as a verified payment method.

## Money rules

| Rule | Where |
|---|---|
| Minimum deposit $2 | `MIN_DEPOSIT_USD` |
| Minimum withdrawal $5, plus a $1 fee on top ($6 leaves the balance) | `MIN_WITHDRAW_USD`, `WITHDRAW_FEE_USD` |
| Card deposits keep 1.5% | `CARD_FEE_PCT` |
| M-Pesa amounts converted at KSh 129.5 to $1 | `USD_KES_RATE` (deposits round the shillings up, payouts round them down) |
| Withdrawals only to verified methods | An M-Pesa number verifies on its first successful deposit; bank and USDT are verified by hand (`sql/admin/payment_methods.sql`); cards cannot receive withdrawals |

Every balance change is one SQL statement that also moves the transaction out of its open state, so a callback that arrives twice, or races a status check, settles the payment once.

## Endpoints

All JSON. Signed-in routes take `Authorization: Bearer <access_token>`. Errors are `{"detail": "a sentence for the user"}`.

| Method | Path | |
|---|---|---|
| POST | `/auth/signup` | `{name, email, password, referral_code?}` → a session, or `{confirm_email: true}` |
| POST | `/auth/login` | `{email, password}` → `{access_token, refresh_token, expires_at, profile}` |
| POST | `/auth/refresh` | `{refresh_token}` → a new session |
| POST | `/auth/logout` | ends the session |
| POST | `/auth/password/forgot` | `{email}` → sends a reset link to `/login?reset=1` |
| POST | `/auth/password/update` | `{password}`, signed in (used from the reset link) |
| GET | `/auth/google?ref=` | `{url}` to send the browser to |
| GET · PATCH | `/me` | the profile · update `{name, phone, country_code, country_name, currency}` |
| GET | `/accounts` | demo and real balances |
| GET · POST | `/payment-methods` | list · add `{kind: mpesa, phone}` / `{kind: bank, bank_name, account_number, name}` / `{kind: usdt, address}` |
| DELETE | `/payment-methods/{id}` | remove |
| POST | `/payment-methods/{id}/default` | make default |
| GET | `/referrals/link` | `{code, link}`; every account has one from the start |
| GET | `/referrals` | who joined with your code |
| GET | `/referrals/check/{code}` | `{valid}`, for the sign-up form |
| POST | `/referrals/claim` | `{code}`, attach a referrer after a Google sign-up (within 24 hours) |
| GET | `/rates` | the conversion rate and the limits above |
| POST | `/payments/deposit/mpesa` | `{amount_usd, phone? , payment_method_id?}` → sends the STK push |
| POST | `/payments/deposit/card` | `{amount_usd}` → `{authorization_url}` to send the browser to |
| POST | `/payments/withdraw` | `{amount_usd, payment_method_id}` |
| GET | `/payments/{reference}` | one payment's state; polls the provider while it is open |
| GET | `/transactions` | history, newest first |
| POST | `/webhooks/payhero`, `/webhooks/paystack` | provider callbacks |
| GET | `/health` | database and provider readiness |

`/docs` (the interactive API explorer) is on only when `ENV=development`.

## Run locally

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env            # then fill in DATABASE_URL at least
uvicorn app.main:app --reload
python -m pip install pytest && python -m pytest -q    # offline tests, no keys needed
```

To receive provider callbacks on your machine, expose port 8000 with a tunnel (e.g. `cloudflared tunnel --url http://localhost:8000`) and set `PUBLIC_API_URL` to the tunnel address.

## Not built yet

Trading (contracts, settlement, demo balance changes), referral earnings, KYC document review, and the Academy checkout run on the UI's local simulation for now. `/referrals/earnings` answers empty until earnings exist.
