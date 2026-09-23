"""Referral links. Every account gets a code when its profile is created;
the link is FRONTEND_URL/r/<code>, which the UI sends to sign-up."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..security import AuthUser, current_user
from ..services import profiles
from ..util import show_day

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
    """Everyone who joined with this user's code. Earnings arrive with the
    trading service; until then volume and earned are zero."""
    rows = await db.many(
        """select p.email, p.created_at,
                  exists (select 1 from app.transactions t
                           where t.user_id = p.id and t.type = 'deposit' and t.status = 'completed') as funded
             from app.profiles p
            where p.referred_by = %s
            order by p.created_at desc""", (user.id,))
    return [{"user": _mask_email(r["email"]), "joined": show_day(r["created_at"]),
             "status": "Active" if r["funded"] else "Pending", "volume": 0, "earned": 0} for r in rows]


@router.get("/earnings")
async def earnings(user: AuthUser = Depends(current_user)):
    """Null until the first earnings exist, so the UI shows its empty state."""
    return None
