"""Referral links. Every account gets a code when its profile is created;
the link is FRONTEND_URL/r/<code>, which the UI sends to sign-up."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import UndefinedTable
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..security import AuthUser, current_user
from ..services import profiles
from ..services.referrals import payday, period
from ..util import show_day, usd

router = APIRouter(prefix="/referrals", tags=["referrals"])

CLAIM_WINDOW = timedelta(hours=24)


class Claim(BaseModel):
    code: str = Field(min_length=4, max_length=20)


def _mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return (name[:2] + "•••" + ("@" + domain if domain else "")) if name else "•••"


@router.get("/link")
async def link(user: AuthUser = Depends(current_user)):
    p = await profiles.for_user(user)
    s = get_settings()
    return {"code": p["referral_code"], "link": f"{s.frontend_url}/r/{p['referral_code']}"}


@router.get("/check/{code}")
async def check(code: str):
    """For the sign-up form: does this code exist? Says nothing else about it."""
    return {"valid": bool(await profiles.referrer_id(code))}


@router.post("/claim")
async def claim(body: Claim, user: AuthUser = Depends(current_user)):
    """Attach a referrer after the fact, for sign-ups that went through Google.
    Only once, and only within a day of the account being created."""
    p = await profiles.for_user(user)
    if p["referred_by"]:
        return {"claimed": False, "reason": "already referred"}
    if datetime.now(timezone.utc) - p["created_at"] > CLAIM_WINDOW:
        return {"claimed": False, "reason": "too late"}
    ref = await profiles.referrer_id(body.code)
    if not ref or ref == user.id:
        raise HTTPException(status_code=400, detail="That referral code does not exist.")
    n = await db.run("update app.profiles set referred_by = %s where id = %s and referred_by is null", (ref, user.id))
    return {"claimed": n > 0}


@router.get("")
async def referred(user: AuthUser = Depends(current_user)):
    """Everyone who joined with this user's code: whether they have funded an
    account, their real-money volume, and what it has earned the referrer."""
    rows = await db.many(
        """select p.id, p.email, p.created_at,
                  exists (select 1 from app.transactions t
                           where t.user_id = p.id and t.type = 'deposit' and t.status = 'completed') as funded
             from app.profiles p
            where p.referred_by = %s
            order by p.created_at desc""", (user.id,))
    if not rows:
        return []
    try:
        vol = {r["user_id"]: r["volume"] for r in await db.many(
            """select t.user_id, sum(t.stake) as volume from app.trades t
                where t.user_id = any(%s) and t.account_kind = 'real' and t.status <> 'open'
                group by t.user_id""", ([r["id"] for r in rows],))}
        weeks = await db.many(
            "select volume, amount from app.referral_earnings where referrer_id = %s", (user.id,))
    except UndefinedTable:                               # trading or earnings tables not set up yet
        vol, weeks = {}, []
    total_vol = sum((w["volume"] for w in weeks), Decimal("0"))
    total_amt = sum((w["amount"] for w in weeks), Decimal("0"))
    rate = (total_amt / total_vol) if total_vol else Decimal("0")     # what each dollar has earned, overall
    return [{"user": _mask_email(r["email"]), "joined": show_day(r["created_at"]),
             "status": "Active" if r["funded"] else "Pending",
             "volume": float(vol.get(r["id"], 0)),
             "earned": float(usd(vol.get(r["id"], Decimal("0")) * rate))} for r in rows]


@router.get("/earnings")
async def earnings(user: AuthUser = Depends(current_user)):
    """Week by week, newest first. Null until the first week with earnings,
    so the page shows its empty state."""
    try:
        rows = await db.many(
            """select * from app.referral_earnings
                where referrer_id = %s and (amount > 0 or status = 'pending')
                order by week_start desc limit 52""", (user.id,))
    except UndefinedTable:
        return None
    if not any(r["amount"] > 0 for r in rows):
        return None
    paid = sum((r["amount"] for r in rows if r["status"] == "paid"), Decimal("0"))
    pending = sum((r["amount"] for r in rows if r["status"] == "pending"), Decimal("0"))
    return {"paid": float(paid), "pending": float(pending),
            "weeks": [{"period": period(r["week_start"]), "active": r["active"], "volume": float(r["volume"]),
                       "amount": float(r["amount"]), "tier": float(r["tier_pct"]),
                       "status": "Paid" if r["status"] == "paid" else "Pending",
                       "paysOn": payday(r["week_start"]).date().isoformat()} for r in rows]}
