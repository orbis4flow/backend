"""Responsible-trading limits, checked where money moves.

  trading   refused during cooling-off or self-exclusion; a real-money trade
            refused once the day's real losses reach the daily loss limit
  deposits  refused during self-exclusion, and above the daily deposit cap

"Today" is the calendar day in Nairobi. Before sql/005 is run there are no
limits to read, and nothing is refused.
"""
from datetime import datetime, time
from decimal import Decimal

from fastapi import HTTPException
from psycopg.errors import UndefinedTable

from .. import db
from ..util import NAIROBI


def _today_start() -> datetime:
    return datetime.combine(datetime.now(NAIROBI).date(), time.min, NAIROBI)


def _when(t: datetime) -> str:
    return t.astimezone(NAIROBI).strftime("%d %b %Y").lstrip("0")


async def _prefs(user_id: str) -> dict | None:
    try:
        return await db.one("select * from app.preferences where user_id = %s", (user_id,))
    except UndefinedTable:
        return None


async def can_trade(user_id: str, account: str, stake: Decimal) -> None:
    p = await _prefs(user_id)
    if not p:
        return
    now = datetime.now(NAIROBI)
    if p["self_excluded_until"] and p["self_excluded_until"] > now:
        raise HTTPException(status_code=403, detail=f"Your account is self-excluded until {_when(p['self_excluded_until'])}.")
    if p["cooling_off_until"] and p["cooling_off_until"] > now:
        raise HTTPException(status_code=403, detail=f"You are cooling off until {_when(p['cooling_off_until'])}.")
    if account == "real" and p["loss_limit_daily"] is not None:
        row = await db.one(
            """select coalesce(-sum(profit), 0) as lost from app.trades
                where user_id = %s and account_kind = 'real' and status <> 'open' and settled_at >= %s""",
            (user_id, _today_start()))
        if Decimal(row["lost"]) + stake > p["loss_limit_daily"]:
            raise HTTPException(status_code=403, detail=(
                f"This could take today's losses past your daily limit of ${p['loss_limit_daily']:,.2f}. "
                "It resets at midnight."))


async def can_deposit(user_id: str, amount: Decimal) -> None:
    p = await _prefs(user_id)
    if not p:
        return
    now = datetime.now(NAIROBI)
    if p["self_excluded_until"] and p["self_excluded_until"] > now:
        raise HTTPException(status_code=403, detail=f"Your account is self-excluded until {_when(p['self_excluded_until'])}.")
    if p["deposit_limit_daily"] is not None:
        row = await db.one(
            """select coalesce(sum(amount_usd), 0) as today from app.transactions
                where user_id = %s and type = 'deposit' and status in ('pending', 'processing', 'completed')
                  and created_at >= %s""", (user_id, _today_start()))
        left = p["deposit_limit_daily"] - Decimal(row["today"])
        if amount > left:
            raise HTTPException(status_code=403, detail=(
                f"That is over your daily deposit limit. You can deposit ${max(left, Decimal(0)):,.2f} more today."))
