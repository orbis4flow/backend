"""Kora (Korapay): M-Pesa deposits, the default M-Pesa provider.

First an STK push straight to the phone (/charges/mobile-money). If Kora
refuses it, Kora's hosted checkout (/charges/initialize), limited to mobile
money, where the user pays on Kora's page and comes back to the cashier.
Both need only the secret key.

API reference: https://developers.korapay.com
Authentication is a Bearer token: the secret key from the Kora dashboard
(Settings → API Configuration). Amounts are in whole shillings.
Withdrawals are not sent through Kora: they are paid by hand
(sql/admin/withdrawals.sql).

Kora signs each webhook (x-korapay-signature: HMAC-SHA256 of the payload's
`data` object, keyed by the secret key), and the API still re-reads the
charge from /charges/{reference} before any money moves.
"""
import hashlib
import hmac
import json

import httpx

from ..config import get_settings


class KoraError(Exception):
    pass


async def _call(method: str, path: str, **kw) -> dict:
    s = get_settings()
    if not s.kora_ready:
        raise KoraError("M-Pesa payments are not configured yet.")
    try:
        async with httpx.AsyncClient(base_url=s.kora_base_url, timeout=30.0) as c:
            r = await c.request(method, path, headers={"Authorization": f"Bearer {s.kora_secret_key}"}, **kw)
    except httpx.HTTPError as e:
        raise KoraError("M-Pesa is not reachable right now. Try again.") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or body.get("status") is False:
        raise KoraError(str(body.get("message") or f"HTTP {r.status_code}"))
    return body.get("data") or {}


def notification_url() -> str:
    return f"{get_settings().api_url}/webhooks/kora"


async def stk_push(*, amount_kes: int, msisdn: str, reference: str, customer_name: str, email: str) -> dict:
    """Ask the phone to approve a payment. Kora answers at once (status
    'processing'); the result arrives later on the webhook."""
    return await _call("POST", "/charges/mobile-money", json={
        "amount": amount_kes,
        "currency": "KES",
        "reference": reference,
        "notification_url": notification_url(),
        "description": "obisflow deposit",
        "customer": {"name": customer_name or "obisflow client", "email": email},
        "mobile_money": {"number": msisdn},
    })


CHECKOUT_SUFFIX = "-KC"    # a checkout after a refused push gets its own reference at Kora


async def checkout(*, amount_kes: int, reference: str, customer_name: str, email: str, redirect_url: str) -> dict:
    """Kora's hosted payment page, mobile money only. Returns checkout_url."""
    return await _call("POST", "/charges/initialize", json={
        "amount": amount_kes,
        "currency": "KES",
        "reference": reference,
        "redirect_url": redirect_url,
        "notification_url": notification_url(),
        "narration": "obisflow deposit",
        "channels": ["mobile_money"],
        "default_channel": "mobile_money",
        "customer": {"name": customer_name or "obisflow client", "email": email},
    })


def our_reference(kora_reference: str) -> str:
    """The transaction's own reference, from the one Kora knows it by."""
    return kora_reference.removesuffix(CHECKOUT_SUFFIX)


async def status(reference: str) -> dict:
    """The authoritative state of a charge, looked up by our reference."""
    return await _call("GET", f"/charges/{reference}")


def status_says(data: dict) -> str:
    """'success', 'failed' or 'pending' from a charge object."""
    st = str(data.get("status") or "").lower()
    if st == "success":
        return "success"
    if st in ("failed", "cancelled", "canceled", "expired", "reversed"):
        return "failed"
    return "pending"


def signature_ok(payload: dict, signature: str | None) -> bool:
    """Kora signs JSON.stringify(payload.data); json.dumps without spaces matches it."""
    s = get_settings()
    if not signature or not s.kora_secret_key or not isinstance(payload.get("data"), dict):
        return False
    signed = json.dumps(payload["data"], separators=(",", ":"), ensure_ascii=False).encode()
    expected = hmac.new(s.kora_secret_key.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
