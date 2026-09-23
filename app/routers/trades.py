"""Contracts: placing, settling, selling back, and the reports built on them.

Every balance change is one transaction with the trade row it belongs to:
placing takes the stake, settling pays back what the contract returned,
selling back pays 75% of the stake. A trade settles once; asking again
answers with the same result.

Demo only. The outcome is decided from the entry and exit prices the
client's chart saw, which is fine for practice money and not for real
money: real trading waits for a server-side price feed (README, "Next").
"""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import UndefinedTable
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..security import AuthUser, current_user
from ..services import limits, profiles
from ..util import code, show_date, usd

router = APIRouter(tags=["trading"])

SELL_BACK = Decimal("0.75")       # the share of the stake a contract sold back returns
GRACE = timedelta(seconds=30)     # after expiry, an unsettled contract is settled by the server
EARLY = timedelta(seconds=1.5)    # clock skew allowed when the client settles at expiry
MIN_STAKE, MAX_STAKE = Decimal("1"), Decimal("10000")


class Place(BaseModel):
    account: Literal["demo", "real"] = "demo"
    symbol: str = Field(min_length=1, max_length=32)
    direction: Literal["Rise", "Fall"]
    stake: float = Field(gt=0)
    payout_pct: float = Field(gt=0, le=100)
    duration_s: int = Field(ge=1, le=3600)
    entry_price: str = Field(min_length=1, max_length=32)


class Settle(BaseModel):
    exit_price: str = Field(min_length=1, max_length=32)


def _price(text: str) -> Decimal:
    try:
        p = Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="That price could not be read.")
    if not p.is_finite() or p <= 0:
        raise HTTPException(status_code=400, detail="That price could not be read.")
    return p


def _num(p: Decimal | None) -> str | None:
    """A price as the chart shows it: no trailing zeros past the quote."""
    if p is None:
        return None
    return format(p.normalize(), "f")


def public(t: dict, balance: Decimal | None = None) -> dict:
    out = {
        "id": t["ref"], "sym": t["symbol"], "dir": t["direction"],
        "stake": float(t["stake"]), "payout": float(t["payout_pct"]),
        "entry": _num(t["entry_price"]), "exit": _num(t["exit_price"]),
        "status": t["status"], "result": t["status"] if t["status"] != "sold" else
                  ("won" if (t["profit"] or 0) >= 0 else "lost"),
        "returned": float(t["returned"]), "profit": float(t["profit"]) if t["profit"] is not None else None,
        "pl": float(t["profit"] or 0), "account": t["account_kind"],
        "openedAt": t["opened_at"].isoformat(), "expiresAt": t["expires_at"].isoformat(),
        "settledAt": t["settled_at"].isoformat() if t["settled_at"] else None,
        "when": show_date(t["settled_at"] or t["opened_at"]),
    }
    if balance is not None:
        out["balance"] = float(balance)
    return out


async def _settle(conn, t: dict, *, exit_price: Decimal | None, by: str) -> tuple[dict, Decimal]:
    """Settle an open trade inside the caller's transaction. With no exit
    price (the client never came back), the server decides it at even odds."""
    if exit_price is None:
        won = secrets.randbelow(2) == 1
    elif t["direction"] == "Rise":
        won = exit_price > t["entry_price"]
    else:
        won = exit_price < t["entry_price"]
    stake = t["stake"]
    returned = usd(stake + stake * t["payout_pct"] / 100) if won else Decimal("0.00")
    cur = await conn.execute(
        """update app.trades
              set status = %s, exit_price = %s, returned = %s, profit = %s,
                  settled_by = %s, settled_at = now()
            where id = %s and status = 'open'
        returning *""",
        ("won" if won else "lost", exit_price, returned, returned - stake, by, t["id"]))
    row = await cur.fetchone()
    cur = await conn.execute(
        "update app.accounts set balance = balance + %s where id = %s returning balance",
        (returned if row else Decimal("0"), t["account_id"]))
    bal = (await cur.fetchone())["balance"]
    return row or t, bal


async def sweep(user_id: str) -> None:
    """Contracts nobody came back to settle (a closed tab) are settled by
    the server once they are well past expiry, so no stake is left hanging.
    Quietly does nothing before sql/003_trades.sql has been run, so reading
    balances never fails because trading is not set up yet."""
    try:
        await _sweep(user_id)
    except UndefinedTable:
        return


async def _sweep(user_id: str) -> None:
    async with db.tx() as conn:
        cur = await conn.execute(
            """select * from app.trades
                where user_id = %s and status = 'open' and expires_at < %s
                for update skip locked""",
            (user_id, datetime.now(timezone.utc) - GRACE))
        for t in await cur.fetchall():
            await _settle(conn, t, exit_price=None, by="expired")


async def _mine(conn, ref: str, user_id: str) -> dict:
    cur = await conn.execute("select * from app.trades where ref = %s and user_id = %s for update", (ref, user_id))
    t = await cur.fetchone()
    if not t:
        raise HTTPException(status_code=404, detail="That contract was not found.")
    return t


# ------------------------------------------------------------------ place --
@router.post("/trades", status_code=201)
async def place(body: Place, user: AuthUser = Depends(current_user)):
    if body.account == "real":
        raise HTTPException(status_code=403,
                            detail="Real-money trading is not open yet. Switch to Demo to practise.")
    await profiles.for_user(user)
    stake = usd(body.stake)
    if stake < MIN_STAKE or stake > MAX_STAKE:
        raise HTTPException(status_code=400, detail=f"Stake must be between ${MIN_STAKE} and ${MAX_STAKE:,}.")
    entry = _price(body.entry_price)
    await limits.can_trade(user.id, body.account, stake)

    async with db.tx() as conn:
        cur = await conn.execute(
            """update app.accounts set balance = balance - %s
                where user_id = %s and kind = %s and balance >= %s
            returning id, balance""",
            (stake, user.id, body.account, stake))
        acct = await cur.fetchone()
        if not acct:
            raise HTTPException(status_code=400, detail="Not enough balance for that stake.")
        cur = await conn.execute(
            """insert into app.trades (ref, user_id, account_id, account_kind, symbol, direction, stake,
                                       payout_pct, duration_s, entry_price, expires_at)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
            returning *""",
            ("OB-" + code(7), user.id, acct["id"], body.account, body.symbol.strip(), body.direction, stake,
             Decimal(str(body.payout_pct)), body.duration_s, entry, body.duration_s))
        t = await cur.fetchone()
    return public(t, acct["balance"])


@router.post("/trades/{ref}/settle")
async def settle(ref: str, body: Settle, user: AuthUser = Depends(current_user)):
    exit_price = _price(body.exit_price)
    async with db.tx() as conn:
        t = await _mine(conn, ref, user.id)
        if t["status"] != "open":                       # already settled: same answer again
            cur = await conn.execute("select balance from app.accounts where id = %s", (t["account_id"],))
            return public(t, (await cur.fetchone())["balance"])
        if datetime.now(timezone.utc) < t["expires_at"] - EARLY:
            raise HTTPException(status_code=409, detail="This contract has not expired yet.")
        row, bal = await _settle(conn, t, exit_price=exit_price, by="price")
    return public(row, bal)


@router.post("/trades/{ref}/sell")
async def sell(ref: str, user: AuthUser = Depends(current_user)):
    async with db.tx() as conn:
        t = await _mine(conn, ref, user.id)
        if t["status"] != "open":
            raise HTTPException(status_code=409, detail="This contract has already settled.")
        returned = usd(t["stake"] * SELL_BACK)
        cur = await conn.execute(
            """update app.trades set status = 'sold', returned = %s, profit = %s,
                      settled_by = 'sold', settled_at = now()
                where id = %s and status = 'open' returning *""",
            (returned, returned - t["stake"], t["id"]))
        row = await cur.fetchone()
        cur = await conn.execute("update app.accounts set balance = balance + %s where id = %s returning balance",
                                 (returned, t["account_id"]))
        bal = (await cur.fetchone())["balance"]
    return public(row, bal)


# ---------------------------------------------------------------- reading --
def _ends(t: dict) -> str:
    left = max(0, int((t["expires_at"] - datetime.now(timezone.utc)).total_seconds()))
    return f"{left // 60:02d}:{left % 60:02d}"


@router.get("/trades/open")
async def open_trades(user: AuthUser = Depends(current_user)):
    await sweep(user.id)
    rows = await db.many("select * from app.trades where user_id = %s and status = 'open' order by opened_at desc",
                         (user.id,))
    return [dict(public(t), now=_num(t["entry_price"]), ends=_ends(t), pl=0.0) for t in rows]


@router.get("/trades/closed")
async def closed_trades(user: AuthUser = Depends(current_user)):
    await sweep(user.id)
    rows = await db.many(
        "select * from app.trades where user_id = %s and status <> 'open' order by settled_at desc limit 200",
        (user.id,))
    return [public(t) for t in rows]


@router.get("/confirmations")
async def confirmations(user: AuthUser = Depends(current_user)):
    return await closed_trades(user)


@router.get("/trades/stats")
async def stats(user: AuthUser = Depends(current_user)):
    by_market = await db.many(
        """select symbol, sum(profit) as pl from app.trades
            where user_id = %s and status <> 'open' group by symbol order by sum(profit) desc""", (user.id,))
    if not by_market:
        return None
    by_duration = await db.many(
        """select case when duration_s < 60 then 'Under 1 minute'
                       when duration_s <= 300 then '1-5 minutes'
                       when duration_s <= 1800 then '5-30 minutes'
                       else 'Over 30 minutes' end as band,
                  min(duration_s) as o, count(*) as n,
                  round(100.0 * count(*) filter (where profit > 0) / count(*)) as win
             from app.trades where user_id = %s and status <> 'open'
            group by band order by o""", (user.id,))
    return {"byMarket": [[r["symbol"], float(r["pl"])] for r in by_market],
            "byDuration": [[r["band"], r["n"], int(r["win"])] for r in by_duration]}


@router.get("/reports/profit")
async def profit_table(user: AuthUser = Depends(current_user)):
    rows = await db.many(
        """select (settled_at at time zone 'Africa/Nairobi')::date as day,
                  count(*) as trades,
                  count(*) filter (where profit > 0) as won,
                  count(*) filter (where profit <= 0) as lost,
                  sum(stake) as turnover, sum(profit) as pl
             from app.trades where user_id = %s and status <> 'open'
            group by day order by day desc limit 90""", (user.id,))
    return [{"date": r["day"].strftime("%d %b %Y").lstrip("0"), "trades": r["trades"], "won": r["won"],
             "lost": r["lost"], "turnover": float(r["turnover"]), "pl": float(r["pl"])} for r in rows]


# ------------------------------------------------------------- demo reset --
@router.post("/accounts/demo/reset")
async def reset_demo(user: AuthUser = Depends(current_user)):
    """Top the practice account back up, once nothing is running on it."""
    await profiles.for_user(user)
    await sweep(user.id)
    s = get_settings()
    async with db.tx() as conn:
        cur = await conn.execute(
            "select 1 from app.trades where user_id = %s and account_kind = 'demo' and status = 'open' limit 1",
            (user.id,))
        if await cur.fetchone():
            raise HTTPException(status_code=409, detail="Wait for your open demo contracts to settle first.")
        cur = await conn.execute(
            "update app.accounts set balance = %s where user_id = %s and kind = 'demo' returning balance",
            (Decimal(str(s.demo_balance_usd)), user.id))
        row = await cur.fetchone()
    return {"kind": "demo", "balance": float(row["balance"])}
