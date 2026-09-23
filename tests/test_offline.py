"""Tests that need no database, no Supabase and no provider keys.

    python -m pytest -q
"""
import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import util
from app.config import get_settings
from app.main import app
from app.routers import auth as auth_router
from app.services import payhero, paystack, profiles

client = TestClient(app)          # not used as a context manager, so no database pool opens


# --------------------------------------------------------------------- util --
@pytest.mark.parametrize("raw,want", [
    ("0712345678", "254712345678"), ("+254 712 345 678", "254712345678"),
    ("254112345678", "254112345678"), ("712345678", "254712345678"),
    ("0812345678", None), ("12345", None), ("", None),
])
def test_kenyan_numbers(raw, want):
    assert util.kenyan_msisdn(raw) == want


def test_phone_forms():
    assert util.local_phone("254712345678") == "0712345678"
    assert util.mask_phone("254712345412") == "+254 7•• ••• 412"


def test_kes_rounding_never_shortchanges():
    # deposits round the shillings up, payouts round them down
    assert util.kes_up(Decimal("2.00"), 129.5) == 259
    assert util.kes_up(Decimal("2.01"), 129.5) == 261       # 260.295 → 261
    assert util.kes_down(Decimal("5.00"), 129.5) == 647     # 647.5 → 647


def test_codes_avoid_lookalikes():
    for _ in range(200):
        c = util.referral_code()
        assert c.startswith("ORBIS-") and len(c) == 11
        assert not set(c[6:]) & set("01OIL")
    assert util.reference("DP").startswith("DP-")


def test_tron_address():
    assert util.TRON_ADDRESS.fullmatch("TXqLJrvZc9ouyVPai66WR55dvDVetR83BH")
    assert not util.TRON_ADDRESS.fullmatch("0xabc")


# ------------------------------------------------------------------ payhero --
def test_payhero_stk_callback():
    info = payhero.parse_callback({"status": True, "response": {
        "Amount": 259, "CheckoutRequestID": "ws_CO_1", "ExternalReference": "DP-ABC",
        "MpesaReceiptNumber": "SAE3YULR0Y", "ResultCode": 0, "ResultDesc": "ok", "Status": "Success"}})
    assert info["reference"] == "DP-ABC" and info["ok"] and not info["failed"]
    assert info["receipt"] == "SAE3YULR0Y" and info["amount"] == 259


def test_payhero_failed_callback():
    info = payhero.parse_callback({"status": False, "response": {
        "ExternalReference": "DP-ABC", "ResultCode": 1032, "ResultDesc": "Request cancelled by user",
        "Status": "Failed"}})
    assert not info["ok"] and info["failed"] and "cancelled" in info["reason"]


def test_payhero_status_words():
    assert payhero.status_says({"status": "SUCCESS"}) == "success"
    assert payhero.status_says({"status": "FAILED"}) == "failed"
    assert payhero.status_says({"status": "QUEUED"}) == "pending"


# ----------------------------------------------------------------- paystack --
def test_paystack_signature(monkeypatch):
    monkeypatch.setattr(get_settings(), "paystack_secret_key", "sk_test_x")
    body = b'{"event":"charge.success"}'
    good = hmac.new(b"sk_test_x", body, hashlib.sha512).hexdigest()
    assert paystack.signature_ok(body, good)
    assert not paystack.signature_ok(body, "0" * 128)
    assert not paystack.signature_ok(body, None)


# ---------------------------------------------------------------------- api --
def test_rates_reflect_the_rules():
    r = client.get("/rates").json()
    assert r["minDepositUsd"] == 2 and r["minWithdrawUsd"] == 5 and r["withdrawFeeUsd"] == 1


def test_signed_in_routes_need_a_token():
    for path in ("/me", "/accounts", "/payment-methods", "/referrals/link", "/transactions",
                 "/trades/open", "/trades/closed", "/trades/stats", "/reports/profit", "/confirmations",
                 "/preferences", "/sessions"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.json()["detail"]


def test_validation_is_one_sentence():
    r = client.post("/auth/signup", json={"name": "A", "email": "nope", "password": "short"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)


def test_google_off_says_so(monkeypatch):
    async def off(provider):
        return False
    monkeypatch.setattr(auth_router.supabase_auth, "provider_enabled", off)
    r = client.get("/auth/google")
    assert r.status_code == 503 and "Google" in r.json()["detail"]


def test_google_url_points_back_to_login(monkeypatch):
    async def on(provider):
        return True
    monkeypatch.setattr(auth_router.supabase_auth, "provider_enabled", on)
    url = client.get("/auth/google", params={"ref": "orbis-abcde"}).json()["url"]
    assert "/auth/v1/authorize" in url and "provider=google" in url
    assert "ORBIS-ABCDE" in url


def test_payhero_webhook_needs_the_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "payhero_callback_token", "secret-token")
    assert client.post("/webhooks/payhero?token=wrong", json={}).status_code == 403


def test_paystack_webhook_needs_a_signature(monkeypatch):
    monkeypatch.setattr(get_settings(), "paystack_secret_key", "sk_test_x")
    r = client.post("/webhooks/paystack", content=json.dumps({"event": "charge.success"}),
                    headers={"x-paystack-signature": "bad"})
    assert r.status_code == 401


# ------------------------------------------------ sign-up, Supabase mocked --
def test_signup_rejects_an_unknown_referral_code(monkeypatch):
    async def none(code, conn=None):
        return None
    monkeypatch.setattr(profiles, "referrer_id", none)
    r = client.post("/auth/signup", json={"name": "Ada", "email": "ada@example.com",
                                          "password": "longenough", "referral_code": "ORBIS-ZZZZZ"})
    assert r.status_code == 400 and "referral" in r.json()["detail"].lower()


def test_signup_with_email_confirmation(monkeypatch):
    made = {}

    async def fake_sign_up(email, password, data, redirect_to):
        return {"id": "u1", "email": email, "identities": [{"id": "i1"}]}

    async def fake_ensure(uid, email, name=None, ref=None):
        made.update(uid=uid, email=email, name=name)
        return {}

    monkeypatch.setattr(auth_router.supabase_auth, "sign_up", fake_sign_up)
    monkeypatch.setattr(profiles, "ensure", fake_ensure)
    r = client.post("/auth/signup", json={"name": "Ada", "email": "ada@example.com", "password": "longenough"})
    assert r.status_code == 200 and r.json() == {"confirm_email": True, "email": "ada@example.com"}
    assert made == {"uid": "u1", "email": "ada@example.com", "name": "Ada"}


def test_signup_existing_email(monkeypatch):
    async def fake_sign_up(email, password, data, redirect_to):
        return {"id": "u1", "email": email, "identities": []}
    monkeypatch.setattr(auth_router.supabase_auth, "sign_up", fake_sign_up)
    r = client.post("/auth/signup", json={"name": "Ada", "email": "ada@example.com", "password": "longenough"})
    assert r.status_code == 409


def test_trading_routes_need_a_token():
    assert client.post("/trades", json={"symbol": "EUR/USD", "direction": "Rise", "stake": 10,
                                        "payout_pct": 88, "duration_s": 6, "entry_price": "1.08"}).status_code == 401
    assert client.post("/trades/OB-XXXXXXX/settle", json={"exit_price": "1.09"}).status_code == 401
    assert client.post("/accounts/demo/reset").status_code == 401


def test_prices_are_read_like_the_chart_writes_them():
    from app.routers.trades import _num, _price
    assert _price("18,642.10") == Decimal("18642.10")
    assert _num(_price("1.08420")) == "1.0842"
    with pytest.raises(Exception):
        _price("abc")


# -------------------------------------------------- usdt, referrals, email --
def test_usdt_exact_amounts_carry_cents():
    from app.services.tron import MICRO, exact_amount
    for _ in range(300):
        a = exact_amount(Decimal("50.00"))
        assert Decimal("50.01") <= a <= Decimal("50.99") and a == a.quantize(Decimal("0.01"))
    # a transfer of 50.37 USDT (6 decimals on chain) matches a request stored as 50.37
    assert Decimal("50370000") / MICRO == Decimal("50.37")
    assert {Decimal("50.37"): 1}.get(Decimal("50370000") / MICRO) == 1


def test_referral_weeks_tiers_and_payday():
    from datetime import date
    from app.services.referrals import payday, period, tier_for, week_of
    assert week_of(date(2026, 9, 23)) == date(2026, 9, 21)          # a Wednesday -> its Monday
    assert payday(date(2026, 9, 14)).date() == date(2026, 9, 24)      # week of the 14th pays Thursday 24th
    assert period(date(2026, 9, 14)) == "14-20 Sep 2026"
    assert period(date(2026, 9, 28)) == "28 Sep - 4 Oct 2026"
    assert (tier_for(0), tier_for(1), tier_for(10), tier_for(50)) == (0, 20, 28, 35)


def test_email_page_escapes_and_brands():
    from app.services.notify import _page
    html = _page("Deposit <b>", ["Hello"], [("Credited", "$5.00")], ("Open", "https://x.test/cashier"))
    assert "Deposit &lt;b&gt;" in html and "$5.00" in html and "https://x.test/cashier" in html


def test_market_routes_are_public_and_empty_without_data(monkeypatch):
    from app.routers import market
    async def nothing():
        return []
    monkeypatch.setattr(market, "_cache", {})
    monkeypatch.setattr(market, "_fetch_news", nothing)
    monkeypatch.setattr(market, "_fetch_calendar", nothing)
    assert client.get("/news").json() is None
    assert client.get("/calendar", params={"range": "week"}).json() is None



# ------------------------------------------------ preferences and security --
def test_security_routes_need_a_token():
    for method, path, body in [("post", "/security/password", {"current_password": "a", "new_password": "longenough"}),
                               ("post", "/security/2fa/start", {"channel": "email"}),
                               ("post", "/auth/2fa/verify", {"code": "123456"}),
                               ("post", "/auth/2fa/send", None),
                               ("post", "/sessions/revoke-others", None),
                               ("put", "/preferences/limits", {"deposit_daily": 100})]:
        r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert r.status_code == 401, path


def test_codes_are_hashed_salted_and_masked():
    from app.services.otp import _hash, mask
    assert _hash("aa", "123456") != _hash("bb", "123456")
    assert len(_hash("aa", "123456")) == 64
    assert mask("email", "amara@mail.com") == "am•••@mail.com"
    assert mask("sms", "+254712345412") == "+254•••412"


def test_devices_are_named_from_the_browser():
    from app.services.sessions import device_of
    assert device_of("Mozilla/5.0 (Windows NT 10.0) AppleWebKit Chrome/140 Safari/537") == "Chrome · Windows"
    assert device_of("Mozilla/5.0 (iPhone; CPU iPhone OS 18) AppleWebKit Version/18 Mobile Safari/604") == "Safari · iOS"
    assert device_of("") == "Browser"


def test_pause_rules(monkeypatch):
    from app.routers import account
    from app import security
    async def fake_user(token):
        return security.AuthUser(id="u1", email="a@b.co", token=token)
    async def no_check(*a, **k):
        return None
    monkeypatch.setattr(security, "verify", fake_user)
    monkeypatch.setattr("app.services.sessions.check", no_check)
    h = {"Authorization": "Bearer x"}
    r = client.post("/preferences/pause", json={"kind": "cooling_off", "days": 60}, headers=h)
    assert r.status_code == 400 and "42 days" in r.json()["detail"]
    r = client.post("/preferences/pause", json={"kind": "self_exclusion", "days": 30}, headers=h)
    assert r.status_code == 400 and "six months" in r.json()["detail"]
