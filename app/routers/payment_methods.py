"""Where money comes from and goes to.

M-Pesa numbers are verified by the first successful deposit from them, and
cards are saved (verified) by a successful card payment through Paystack.
Bank accounts and USDT wallets wait for a person to verify them (see
sql/admin/payment_methods.sql). Withdrawals only go to verified methods.
"""
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import db
from ..security import AuthUser, current_user
from ..services import notify, profiles
from ..util import TRON_ADDRESS, kenyan_msisdn, mask_phone, mask_tail, mask_wallet

router = APIRouter(prefix="/payment-methods", tags=["payment methods"])

MAX_METHODS = 10


class NewMethod(BaseModel):
    kind: Literal["mpesa", "bank", "usdt"]
    phone: str | None = Field(default=None, max_length=20)          # mpesa
    name: str | None = Field(default=None, max_length=80)           # name on the account
    bank_name: str | None = Field(default=None, max_length=60)      # bank
    account_number: str | None = Field(default=None, max_length=34)
    address: str | None = Field(default=None, max_length=64)        # usdt


def public(r: dict) -> dict:
    return {
        "id": str(r["id"]), "kind": r["kind"], "label": r["label"], "masked": r["masked"],
        "status": r["status"], "isDefault": r["is_default"],
        "createdAt": r["created_at"].isoformat(),
    }


async def save_method(user_id: str, kind: str, label: str, masked: str, fingerprint: str,
                      details: dict, conn=None, *, verified: bool = False) -> dict:
    """Add a method, or bring back one the user removed earlier. A method an
    admin blocked stays blocked."""
    row = await db.one(
        """insert into app.payment_methods (user_id, kind, label, masked, fingerprint, details, status)
           values (%(u)s, %(k)s, %(l)s, %(m)s, %(f)s, %(d)s, %(st)s)
           on conflict (user_id, kind, fingerprint) do update
             set removed_at = null,
                 label = excluded.label, masked = excluded.masked,
                 details = app.payment_methods.details || excluded.details,
                 status = case when app.payment_methods.status = 'pending' and excluded.status = 'verified'
                               then 'verified' else app.payment_methods.status end
           returning *""",
        {"u": user_id, "k": kind, "l": label, "m": masked, "f": fingerprint, "d": Jsonb(details),
         "st": "verified" if verified else "pending"}, conn=conn)
    if row["status"] == "disabled":
        raise HTTPException(status_code=403, detail="This payment method cannot be used. Contact support.")
    return row


async def upsert_mpesa(user_id: str, msisdn: str, name: str | None = None, conn=None) -> dict:
    """The user's M-Pesa method for this number, added if it is new."""
    return await save_method(user_id, "mpesa", "M-Pesa", mask_phone(msisdn), msisdn,
                             {"msisdn": msisdn, "name": name} if name else {"msisdn": msisdn}, conn)


@router.get("")
async def list_methods(user: AuthUser = Depends(current_user)):
    rows = await db.many(
        """select * from app.payment_methods
            where user_id = %s and status <> 'disabled' and removed_at is null
            order by is_default desc, created_at""", (user.id,))
    return [public(r) for r in rows]


@router.post("", status_code=201)
async def add_method(body: NewMethod, user: AuthUser = Depends(current_user)):
    p = await profiles.for_user(user)
    n = await db.one(
        """select count(*) as n from app.payment_methods
            where user_id = %s and status <> 'disabled' and removed_at is null""", (user.id,))
    if n["n"] >= MAX_METHODS:
        raise HTTPException(status_code=400, detail=f"You can keep up to {MAX_METHODS} payment methods. Remove one first.")

    holder = (body.name or p["full_name"] or "").strip() or None

    if body.kind == "mpesa":
        msisdn = kenyan_msisdn(body.phone or "")
        if not msisdn:
            raise HTTPException(status_code=400, detail="Enter a Safaricom number, like 0712 345 678.")
        row = await upsert_mpesa(user.id, msisdn, holder)
        notify._later(notify.method_added(user.id, row["label"], row["masked"]))
        return public(row)

    if body.kind == "bank":
        acct = re.sub(r"[\s-]", "", body.account_number or "")
        if not re.fullmatch(r"[0-9A-Za-z]{6,34}", acct):
            raise HTTPException(status_code=400, detail="Enter the full account number.")
        bank = (body.bank_name or "").strip()
        if len(bank) < 2:
            raise HTTPException(status_code=400, detail="Enter the name of the bank.")
        label, masked, fp = bank, mask_tail(acct), f"{bank.lower()}:{acct}"
        details = {"bank_name": bank, "account_number": acct, "account_name": holder}
    else:
        addr = (body.address or "").strip()
        if not TRON_ADDRESS.fullmatch(addr):
            raise HTTPException(status_code=400, detail="That is not a TRON (TRC-20) address. It starts with T and is 34 characters.")
        label, masked, fp = "USDT (TRC-20)", mask_wallet(addr), addr
        details = {"address": addr, "network": "TRC20"}

    existing = await db.one(
        """select removed_at from app.payment_methods where user_id = %s and kind = %s and fingerprint = %s""",
        (user.id, body.kind, fp))
    if existing and existing["removed_at"] is None:
        raise HTTPException(status_code=409, detail="You have already added this one.")
    row = await save_method(user.id, body.kind, label, masked, fp, details)
    notify._later(notify.method_added(user.id, row["label"], row["masked"]))
    return public(row)


@router.delete("/{method_id}", status_code=204)
async def remove_method(method_id: str, user: AuthUser = Depends(current_user)):
    # kept on record, since past transactions point at it, but no longer usable
    n = await db.run(
        """update app.payment_methods set removed_at = now(), is_default = false
            where id = %s and user_id = %s and removed_at is null""", (method_id, user.id))
    if not n:
        raise HTTPException(status_code=404, detail="That payment method was not found.")


@router.post("/{method_id}/default")
async def make_default(method_id: str, user: AuthUser = Depends(current_user)):
    async with db.tx() as conn:
        await conn.execute("update app.payment_methods set is_default = false where user_id = %s and is_default",
                           (user.id,))
        cur = await conn.execute(
            """update app.payment_methods set is_default = true
                where id = %s and user_id = %s and status <> 'disabled' and removed_at is null
            returning *""", (method_id, user.id))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="That payment method was not found.")
    return public(row)
