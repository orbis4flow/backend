"""Turning a provider's answer into a settled transaction.

Used by both the webhooks and the status poll, so a payment settles the same
way whichever arrives first. Nothing here trusts a callback's word for it:
PayHero outcomes are re-read from /transaction-status and Paystack outcomes
from /transaction/verify before any balance changes.
"""
import logging
from decimal import Decimal

from .. import db
from . import ledger, payhero, paystack

log = logging.getLogger("orbisflow.settle")


async def by_reference(reference: str) -> dict | None:
    return await db.one("select * from app.transactions where reference = %s", (reference,))


# ------------------------------------------------------------------ payhero --
async def payhero_check(tx: dict, hint: dict | None = None) -> str:
    """Ask PayHero what happened to tx and settle it. Returns the new state
    ('completed', 'failed', or the unchanged one when PayHero cannot say yet)."""
    pref = tx["provider_ref"] or (hint or {}).get("provider_ref")
    if not pref:
        return tx["status"]
    try:
        body = await payhero.status(pref)
    except payhero.PayHeroError as e:
        log.warning("payhero status failed for %s: %s", tx["reference"], e)
        return tx["status"]

    verdict = payhero.status_says(body)
    receipt = body.get("provider_reference") or body.get("third_party_reference") or (hint or {}).get("receipt")

    if tx["type"] == "deposit":
        if verdict == "success":
            paid = body.get("amount") or (hint or {}).get("amount")
            if paid is not None and tx["local_amount"] is not None and Decimal(str(paid)) < tx["local_amount"]:
                await ledger.fail_deposit(tx["reference"], f"Paid KSh {paid}, expected KSh {tx['local_amount']}")
                return "failed"
            done = await ledger.complete_deposit(tx["reference"], receipt=receipt, provider_ref=pref)
            if done:
                await ledger.verify_method(done["payment_method_id"])
            return "completed"
        if verdict == "failed":
            await ledger.fail_deposit(tx["reference"], (hint or {}).get("reason") or body.get("status_description")
                                      or "M-Pesa payment was not completed")
            return "failed"
        return tx["status"]

    return tx["status"]      # withdrawals are paid by hand, never through PayHero


# ----------------------------------------------------------------- paystack --
async def paystack_apply(data: dict) -> str | None:
    """Settle from a verified Paystack transaction object."""
    tx = await by_reference(data.get("reference") or "")
    if not tx or tx["type"] != "deposit" or tx["provider"] != "paystack":
        return None
    state = str(data.get("status") or "").lower()

    if state == "success":
        want_minor = int(tx["meta"].get("amount_minor") or 0)
        want_ccy = str(tx["meta"].get("currency") or "").upper()
        if int(data.get("amount") or 0) < want_minor or str(data.get("currency") or "").upper() != want_ccy:
            await ledger.fail_deposit(tx["reference"], "Amount or currency did not match")
            log.error("paystack mismatch on %s: %s", tx["reference"], data)
            return "failed"
        done = await ledger.complete_deposit(tx["reference"], receipt=str(data.get("id") or ""),
                                             provider_ref=data.get("reference"))
        if done:
            await _save_card(str(tx["user_id"]), data.get("authorization") or {}, data.get("customer") or {})
        return "completed"

    if state in ("failed", "reversed"):
        await ledger.fail_deposit(tx["reference"], data.get("gateway_response") or "Card payment failed")
        return "failed"
    return tx["status"]          # abandoned / ongoing: the user may still finish paying


async def paystack_check(tx: dict) -> str:
    try:
        data = await paystack.verify(tx["reference"])
    except paystack.PaystackError as e:
        log.warning("paystack verify failed for %s: %s", tx["reference"], e)
        return tx["status"]
    return await paystack_apply(data) or tx["status"]


async def _save_card(user_id: str, auth: dict, customer: dict) -> None:
    """A card that just paid is kept, verified, so the user sees it under payment methods."""
    if not auth.get("reusable") or not auth.get("signature"):
        return
    from ..routers.payment_methods import save_method     # avoid a circular import at load time
    brand = (auth.get("card_type") or auth.get("brand") or "Card").strip().title()
    try:
        await save_method(
            user_id, "card", brand, "••••" + str(auth.get("last4") or ""), auth["signature"],
            {"authorization_code": auth.get("authorization_code"), "bin": auth.get("bin"),
             "last4": auth.get("last4"), "exp_month": auth.get("exp_month"), "exp_year": auth.get("exp_year"),
             "bank": auth.get("bank"), "country_code": auth.get("country_code"),
             "customer_code": customer.get("customer_code")},
            verified=True)
    except Exception:                                      # a card that cannot be saved must not undo a deposit
        log.exception("could not save card for %s", user_id)
