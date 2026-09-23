"""Paystack: card deposits on Paystack's hosted checkout.

Card details never touch orbisflow. We start a transaction, send the user to
Paystack's page, and credit the account when Paystack confirms, either on
the signed webhook or when the user returns and we verify the reference.
"""
import hashlib
import hmac

import httpx

from ..config import get_settings

BASE = "https://api.paystack.co"


class PaystackError(Exception):
    pass


async def _call(method: str, path: str, **kw) -> dict:
    s = get_settings()
    if not s.paystack_ready:
        raise PaystackError("Card payments are not configured yet.")
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as c:
            r = await c.request(method, path, headers={"Authorization": f"Bearer {s.paystack_secret_key}"}, **kw)
    except httpx.HTTPError as e:
        raise PaystackError("The card processor is not reachable right now. Try again.") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or not body.get("status"):
        raise PaystackError(body.get("message") or f"HTTP {r.status_code}")
    return body["data"]


async def initialize(*, email: str, amount_minor: int, currency: str, reference: str,
                     callback_url: str, metadata: dict) -> dict:
    """Returns authorization_url (send the user there), access_code and reference."""
    return await _call("POST", "/transaction/initialize", json={
        "email": email,
        "amount": amount_minor,
        "currency": currency,
        "reference": reference,
        "callback_url": callback_url,
        "metadata": metadata,
        "channels": ["card"],
    })


async def verify(reference: str) -> dict:
    return await _call("GET", f"/transaction/verify/{reference}")


def signature_ok(raw_body: bytes, signature: str | None) -> bool:
    """Paystack signs each webhook with HMAC-SHA512 of the raw body, keyed by the secret key."""
    s = get_settings()
    if not signature or not s.paystack_secret_key:
        return False
    expected = hmac.new(s.paystack_secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)
