"""Deposits (M-Pesa via PayHero, cards via Paystack), withdrawals, and history.

Withdrawals are manual: the balance is taken when the user asks, the request
waits in review, and a person pays it and marks it done (or rejects it and
refunds) with sql/admin/withdrawals.sql.

Limits: deposits from MIN_DEPOSIT_USD, withdrawals from MIN_WITHDRAW_USD
with WITHDRAW_FEE_USD charged on top of the amount.
"""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..security import AuthUser, current_user
from ..services import ledger, payhero, paystack, profiles, settle
from ..util import kenyan_msisdn, kes_down, kes_up, mask_phone, reference, show_date, usd
from .payment_methods import upsert_mpesa

router = APIRouter(tags=["payments"])

METHOD_LABEL = {"mpesa": "M-Pesa", "card": "Card", "bank": "Bank transfer", "usdt": "USDT (TRC-20)"}
STATUS_LABEL = {"pending": "Pending", "processing": "Processing", "review": "In review",
                "completed": "Completed", "failed": "Failed", "cancelled": "Cancelled"}


class MpesaDeposit(BaseModel):
    amount_usd: float = Field(gt=0)
    phone: str | None = Field(default=None, max_length=20)
    payment_method_id: str | None = None


class CardDeposit(BaseModel):
    amount_usd: float = Field(gt=0)


class Withdrawal(BaseModel):
    amount_usd: float = Field(gt=0)
    payment_method_id: str


def public(t: dict) -> dict:
    sign = 1 if t["type"] == "deposit" else -1
    return {
        # for display, as the cashier table reads it
        "when": show_date(t["created_at"]),
        "type": "Deposit" if t["type"] == "deposit" else "Withdrawal",
        "method": METHOD_LABEL[t["method"]],
        "ref": t["reference"],
        "status": STATUS_LABEL[t["status"]],
        "amount": float(t["net_usd"]) * sign,
        # raw, for code
        "reference": t["reference"],
        "kind": t["type"],
        "state": t["status"],
        "amountUsd": float(t["amount_usd"]),
        "feeUsd": float(t["fee_usd"]),
        "netUsd": float(t["net_usd"]),
        "localAmount": float(t["local_amount"]) if t["local_amount"] is not None else None,
        "localCurrency": t["local_currency"],
        "destination": t["destination"],
        "failureReason": t["failure_reason"],
        "createdAt": t["created_at"].isoformat(),
        "completedAt": t["completed_at"].isoformat() if t["completed_at"] else None,
    }


def _deposit_amount(value: float) -> Decimal:
    s = get_settings()
    amount = usd(value)
    if amount < usd(s.min_deposit_usd):
        raise HTTPException(status_code=400, detail=f"The minimum deposit is ${s.min_deposit_usd:,.2f}.")
    if amount > usd(s.max_deposit_usd):
        raise HTTPException(status_code=400, detail=f"The most you can deposit at once is ${s.max_deposit_usd:,.2f}.")
    return amount


async def _insert(conn, **f) -> dict:
    cols = ", ".join(f)
    vals = ", ".join(f"%({k})s" for k in f)
    cur = await conn.execute(f"insert into app.transactions ({cols}) values ({vals}) returning *", f)
    return await cur.fetchone()


@router.get("/rates")
async def rates():
    s = get_settings()
    return {"USD_KES": s.usd_kes_rate, "minDepositUsd": s.min_deposit_usd,
            "minWithdrawUsd": s.min_withdraw_usd, "withdrawFeeUsd": s.withdraw_fee_usd,
            "cardFeePct": s.card_fee_pct}


# ------------------------------------------------------------------ M-Pesa --
@router.post("/payments/deposit/mpesa", status_code=201)
async def deposit_mpesa(body: MpesaDeposit, user: AuthUser = Depends(current_user)):
    s = get_settings()
    if not s.payhero_ready:
        raise HTTPException(status_code=503, detail="M-Pesa deposits are not switched on yet.")
    p = await profiles.for_user(user)
    amount = _deposit_amount(body.amount_usd)

    if body.payment_method_id:
        m = await db.one(
            """select * from app.payment_methods where id = %s and user_id = %s and kind = 'mpesa'
                and removed_at is null and status <> 'disabled'""", (body.payment_method_id, user.id))
        if not m:
            raise HTTPException(status_code=404, detail="That M-Pesa number was not found.")
        msisdn = m["fingerprint"]
    else:
        msisdn = kenyan_msisdn(body.phone or p["phone"] or "")
        if not msisdn:
            raise HTTPException(status_code=400, detail="Enter the Safaricom number to send the prompt to.")

    method = await upsert_mpesa(user.id, msisdn, p["full_name"])
    kes = kes_up(amount, s.usd_kes_rate)
    ref = reference("DP")
    async with db.tx() as conn:
        tx = await _insert(conn, user_id=user.id, account_id=await profiles.real_account_id(user.id, conn),
                           payment_method_id=method["id"], type="deposit", method="mpesa", provider="payhero",
                           amount_usd=amount, fee_usd=Decimal("0"), net_usd=amount, local_amount=kes,
                           local_currency="KES", fx_rate=Decimal(str(s.usd_kes_rate)), reference=ref,
                           destination=mask_phone(msisdn), meta=Jsonb({}))

    try:
        res = await payhero.stk_push(amount_kes=kes, msisdn=msisdn, reference=ref,
                                     customer_name=p["full_name"] or p["email"])
    except payhero.PayHeroError as e:
        await ledger.fail_deposit(ref, str(e))
        raise HTTPException(status_code=502, detail=f"The M-Pesa prompt could not be sent: {e}")

    tx = await db.one(
        """update app.transactions set status = 'processing', provider_ref = %s, meta = meta || %s
            where reference = %s and status = 'pending' returning *""",
        (res.get("reference") or res.get("CheckoutRequestID"), Jsonb({"stk": res}), ref)) or tx
    return public(tx)


# -------------------------------------------------------------------- card --
@router.post("/payments/deposit/card", status_code=201)
async def deposit_card(body: CardDeposit, user: AuthUser = Depends(current_user)):
    s = get_settings()
    if not s.paystack_ready:
        raise HTTPException(status_code=503, detail="Card deposits are not switched on yet.")
    p = await profiles.for_user(user)
    amount = _deposit_amount(body.amount_usd)
    fee = usd(amount * Decimal(str(s.card_fee_pct)) / 100)
    currency = s.paystack_currency.upper()
    if currency == "USD":
        local, minor = amount, int(amount * 100)
    else:
        kes = kes_up(amount, s.usd_kes_rate)
        local, minor = Decimal(kes), kes * 100

    ref = reference("DP")
    async with db.tx() as conn:
        await _insert(conn, user_id=user.id, account_id=await profiles.real_account_id(user.id, conn),
                      type="deposit", method="card", provider="paystack", amount_usd=amount, fee_usd=fee,
                      net_usd=amount - fee, local_amount=local, local_currency=currency,
                      fx_rate=Decimal(str(s.usd_kes_rate)) if currency != "USD" else None, reference=ref,
                      meta=Jsonb({"amount_minor": minor, "currency": currency}))

    try:
        res = await paystack.initialize(
            email=p["email"], amount_minor=minor, currency=currency, reference=ref,
            callback_url=f"{s.frontend_url}/cashier?deposit={ref}",
            metadata={"user_id": user.id, "reference": ref, "amount_usd": str(amount)})
    except paystack.PaystackError as e:
        await ledger.fail_deposit(ref, str(e))
        raise HTTPException(status_code=502, detail=f"Card checkout could not be opened: {e}")

    await db.run("update app.transactions set provider_ref = %s, meta = meta || %s where reference = %s",
                 (res.get("reference") or ref, Jsonb({"access_code": res.get("access_code")}), ref))
    return {"reference": ref, "authorization_url": res["authorization_url"],
            "amountUsd": float(amount), "feeUsd": float(fee), "creditedUsd": float(amount - fee),
            "localAmount": float(local), "localCurrency": currency}


# ------------------------------------------------------------ withdrawals --
@router.post("/payments/withdraw", status_code=201)
async def withdraw(body: Withdrawal, user: AuthUser = Depends(current_user)):
    s = get_settings()
    await profiles.for_user(user)
    amount = usd(body.amount_usd)
    fee = usd(s.withdraw_fee_usd)
    if amount < usd(s.min_withdraw_usd):
        raise HTTPException(status_code=400,
                            detail=f"The minimum withdrawal is ${s.min_withdraw_usd:,.2f}, plus the ${fee} fee.")
    total = amount + fee

    m = await db.one("select * from app.payment_methods where id = %s and user_id = %s and removed_at is null",
                     (body.payment_method_id, user.id))
    if not m or m["status"] == "disabled":
        raise HTTPException(status_code=404, detail="That payment method was not found.")
    if m["kind"] == "card":
        raise HTTPException(status_code=400, detail="Cards cannot receive withdrawals. Choose M-Pesa, a bank or a wallet.")
    if m["status"] != "verified":
        raise HTTPException(status_code=400, detail=(
            "Make one deposit from this M-Pesa number first. That confirms it is yours."
            if m["kind"] == "mpesa" else "This method is still being verified. It usually takes a working day."))

    kes = kes_down(amount, s.usd_kes_rate) if m["kind"] == "mpesa" else None
    ref = reference("WD")

    async with db.tx() as conn:
        account = await ledger.debit_for_withdrawal(conn, user.id, total)
        if not account:
            raise HTTPException(status_code=400,
                                detail=f"Not enough balance. You need ${total:,.2f}: the amount plus the ${fee} fee.")
        tx = await _insert(conn, user_id=user.id, account_id=account, payment_method_id=m["id"],
                           type="withdrawal", method=m["kind"], provider="manual",
                           status="review", amount_usd=amount, fee_usd=fee,
                           net_usd=total, local_amount=kes, local_currency="KES" if kes is not None else None,
                           fx_rate=Decimal(str(s.usd_kes_rate)) if kes is not None else None,
                           reference=ref, destination=m["masked"], meta=Jsonb({}))

    return public(tx)


# ----------------------------------------------------------------- history --
@router.get("/transactions")
async def transactions(user: AuthUser = Depends(current_user)):
    rows = await db.many(
        "select * from app.transactions where user_id = %s order by created_at desc limit 200", (user.id,))
    return [public(r) for r in rows]


@router.get("/payments/{ref}")
async def payment(ref: str, user: AuthUser = Depends(current_user)):
    """One transaction's state. While it is open, the provider is asked again,
    so the UI can poll this until the payment settles."""
    tx = await db.one("select * from app.transactions where reference = %s and user_id = %s", (ref, user.id))
    if not tx:
        raise HTTPException(status_code=404, detail="That payment was not found.")
    age = (datetime.now(timezone.utc) - tx["created_at"]).total_seconds()
    if tx["status"] in ("pending", "processing") and age > 8:
        if tx["provider"] == "payhero":
            await settle.payhero_check(tx)
        elif tx["provider"] == "paystack":
            await settle.paystack_check(tx)
        tx = await db.one("select * from app.transactions where reference = %s", (ref,))
    out = public(tx)
    if tx["status"] == "completed":
        bal = await db.one("select balance from app.accounts where id = %s", (tx["account_id"],))
        out["balance"] = float(bal["balance"])
    return out
