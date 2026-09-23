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
                 "/trades/open", "/trades/closed", "/trades/stats", "/reports/profit", "/confirmations"):
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
