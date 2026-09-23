"""The signed-in user's profile and accounts."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..security import AuthUser, current_user
from ..services import profiles
from ..util import e164, kenyan_msisdn

router = APIRouter(tags=["profile"])


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    phone: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    country_name: str | None = Field(default=None, max_length=60)
    currency: Literal["USD", "KES", "USDT"] | None = None


@router.get("/me")
async def me(user: AuthUser = Depends(current_user)):
    p = await profiles.for_user(user)
    return profiles.public(p, await profiles.real_account_id(user.id))


@router.patch("/me")
async def update_me(body: ProfileUpdate, user: AuthUser = Depends(current_user)):
    await profiles.for_user(user)
    fields: dict = {}
    if body.name is not None:
        fields["full_name"] = body.name.strip()
    if body.phone is not None:
        if body.phone.strip() == "":
            fields["phone"] = None
        else:
            msisdn = kenyan_msisdn(body.phone)
            digits = "".join(ch for ch in body.phone if ch.isdigit())
            if msisdn:
                fields["phone"] = e164(msisdn)
            elif body.phone.strip().startswith("+") and 8 <= len(digits) <= 15:
                fields["phone"] = "+" + digits          # a non-Kenyan number, kept as given
            else:
                raise HTTPException(status_code=400, detail="That phone number does not look right. Include the country code.")
    if body.country_code is not None:
        fields["country_code"] = body.country_code.lower()
        fields["country_name"] = (body.country_name or "").strip() or None
    if body.currency is not None:
        fields["currency"] = body.currency

    if fields:
        sets = ", ".join(f"{k} = %({k})s" for k in fields)
        fields["id"] = user.id
        await db.run(f"update app.profiles set {sets} where id = %(id)s", fields)
    p = await db.one("select * from app.profiles where id = %s", (user.id,))
    return profiles.public(p, await profiles.real_account_id(user.id))


@router.get("/accounts")
async def accounts(user: AuthUser = Depends(current_user)):
    await profiles.for_user(user)
    return await profiles.accounts(user.id)
