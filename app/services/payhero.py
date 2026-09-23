"""PayHero (Kenya): M-Pesa deposits by STK push.

API reference: https://docs.payhero.co.ke
Authentication is HTTP Basic with the API username and password from the
PayHero dashboard (API Keys); PAYHERO_CHANNEL_ID is the till or paybill the
payments go to. Withdrawals are not sent through PayHero: they are paid by
hand (sql/admin/withdrawals.sql).

PayHero does not sign its callbacks, so a callback is never trusted on its
own: its reference is looked up again with /transaction-status before any
money moves, and the callback URL carries a secret token.
"""
import base64
from typing import Any

import httpx

from ..config import get_settings
from ..util import local_phone


class PayHeroError(Exception):
    pass


def _auth_header() -> str:
    s = get_settings()
    raw = f"{s.payhero_api_username}:{s.payhero_api_password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def _call(method: str, path: str, **kw) -> dict:
    s = get_settings()
    if not s.payhero_ready:
        raise PayHeroError("M-Pesa payments are not configured yet.")
    try:
        async with httpx.AsyncClient(base_url=s.payhero_base_url, timeout=30.0) as c:
            r = await c.request(method, path, headers={"Authorization": _auth_header()}, **kw)
    except httpx.HTTPError as e:
        raise PayHeroError("M-Pesa is not reachable right now. Try again.") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or body.get("success") is False:
        msg = body.get("error_message") or body.get("message") or body.get("error") or f"HTTP {r.status_code}"
        raise PayHeroError(str(msg))
    return body


def callback_url() -> str:
    s = get_settings()
    return f"{s.api_url}/webhooks/payhero?token={s.payhero_callback_token}"


async def stk_push(*, amount_kes: int, msisdn: str, reference: str, customer_name: str) -> dict:
    """Ask the phone to approve a payment. Answers at once with PayHero's own
    reference; the result arrives later on the callback."""
    s = get_settings()
    return await _call("POST", "/payments", json={
        "amount": amount_kes,
        "phone_number": local_phone(msisdn),
        "channel_id": s.payhero_channel_id,
        "provider": "m-pesa",
        "external_reference": reference,
        "customer_name": customer_name or "orbisflow client",
        "callback_url": callback_url(),
    })


async def status(provider_ref: str) -> dict:
    """The authoritative state of a payment: QUEUED, SUCCESS or FAILED."""
    return await _call("GET", "/transaction-status", params={"reference": provider_ref})


def parse_callback(payload: dict) -> dict[str, Any]:
    """Pull what matters out of a callback, whichever of PayHero's shapes it is."""
    r = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    status_text = str(r.get("Status") or r.get("status") or "").lower()
    code = r.get("ResultCode", r.get("result_code"))
    ok = status_text in ("success", "successful", "completed") or code in (0, "0")
    return {
        "reference": r.get("ExternalReference") or r.get("external_reference"),
        "provider_ref": r.get("reference") or r.get("CheckoutRequestID") or payload.get("reference"),
        "receipt": r.get("MpesaReceiptNumber") or r.get("TransactionID") or r.get("third_party_reference"),
        "amount": r.get("Amount") or r.get("amount"),
        "ok": ok,
        "failed": (not ok) and (status_text in ("failed", "cancelled", "canceled") or code not in (None, "")),
        "reason": r.get("ResultDesc") or r.get("result_desc") or r.get("error_message"),
    }


def status_says(body: dict) -> str:
    """'success', 'failed' or 'pending' from a /transaction-status answer."""
    st = str(body.get("status") or "").upper()
    if st in ("SUCCESS", "COMPLETED"):
        return "success"
    if st in ("FAILED", "CANCELLED", "CANCELED", "REVERSED"):
        return "failed"
    return "pending"
