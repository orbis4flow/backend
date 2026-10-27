"""The admin API, for the admin site (a separate app, see its README).

It exists only when ADMIN_TOKEN is set (32+ characters); without it every
path here answers 404. The site sends the token as a bearer token. Wrong
tokens are limited to 10 per 15 minutes per address.

Reads cover users, money, trades and the price feed. The few writes are the
ones that used to be SQL run by hand (sql/admin): approving or rejecting a
withdrawal and verifying or blocking a payment method. Each is recorded in
app.admin_actions (sql/009_admin.sql), and the user is told by email.
"""
import hmac
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..services import notify, prices

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

_misses: dict[str, deque] = defaultdict(deque)
MISS_LIMIT, MISS_WINDOW = 10, 15 * 60


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "")


async def admin(request: Request) -> str:
    token = get_settings().admin_token
    if len(token) < 32:
        raise HTTPException(status_code=404, detail="Not Found")
    ip = _ip(request)
    misses = _misses[ip]
    while misses and misses[0] < time.monotonic() - MISS_WINDOW:
        misses.popleft()
    if len(misses) >= MISS_LIMIT:
        raise HTTPException(status_code=429, detail="Too many wrong tokens. Try again in 15 minutes.")
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not given or not hmac.compare_digest(given.encode(), token.encode()):
        misses.append(time.monotonic())
        raise HTTPException(status_code=401, detail="That admin token is not right.")
    return ip


def _f(v) -> float:
    return float(v or 0)


def _iso(v) -> str | None:
    return v.isoformat() if v else None


async def _log(action: str, target: str, user_id, note: str | None, ip: str, conn=None) -> None:
    await db.run("insert into app.admin_actions (action, target, user_id, note, ip) values (%s, %s, %s, %s, %s)",
                 (action, target, user_id, note, ip), conn=conn)


# ---------------------------------------------------------------- overview --
@router.get("/overview")
async def overview(_: str = Depends(admin)):
    day = datetime.now(timezone.utc) - timedelta(hours=24)
    week = datetime.now(timezone.utc) - timedelta(days=7)
    users = await db.one(
        "select count(*) as n, count(*) filter (where created_at >= %s) as new from app.profiles", (day,))
    money = await db.one(
        """select coalesce(sum(balance) filter (where kind = 'real'), 0) as real_balances
             from app.accounts""")
    tx = await db.one(
        """select
             coalesce(sum(net_usd) filter (where type = 'deposit' and status = 'completed' and completed_at >= %s), 0) as dep_day,
             count(*)               filter (where type = 'deposit' and status = 'completed' and completed_at >= %s) as dep_day_n,
             coalesce(sum(net_usd) filter (where type = 'deposit' and status = 'completed' and completed_at >= %s), 0) as dep_week,
             count(*)               filter (where type = 'withdrawal' and status in ('review', 'processing')) as wd_waiting,
             coalesce(sum(net_usd) filter (where type = 'withdrawal' and status in ('review', 'processing')), 0) as wd_waiting_usd,
             coalesce(sum(amount_usd) filter (where type = 'withdrawal' and status = 'completed' and completed_at >= %s), 0) as wd_week,
             count(*)               filter (where type = 'deposit' and status = 'pending' and created_at < now() - interval '15 minutes') as dep_stuck
           from app.transactions""", (day, day, week, week))
    trades = await db.one(
        """select count(*) as n, coalesce(sum(stake), 0) as volume, coalesce(-sum(profit), 0) as house,
                  count(distinct user_id) as traders
             from app.trades where account_kind = 'real' and status <> 'open' and settled_at >= %s""", (day,))
    open_real = await db.one(
        "select count(*) as n, coalesce(sum(stake), 0) as at_risk from app.trades where account_kind = 'real' and status = 'open'")
    methods = await db.one(
        "select count(*) as n from app.payment_methods where status = 'pending' and removed_at is null")
    snap = prices.snapshot()
    return {
        "users": users["n"], "newUsers24h": users["new"],
        "realBalances": _f(money["real_balances"]),
        "deposits24h": _f(tx["dep_day"]), "deposits24hCount": tx["dep_day_n"], "deposits7d": _f(tx["dep_week"]),
        "depositsStuck": tx["dep_stuck"],
        "withdrawalsWaiting": tx["wd_waiting"], "withdrawalsWaitingUsd": _f(tx["wd_waiting_usd"]),
        "withdrawals7d": _f(tx["wd_week"]),
        "realTrades24h": trades["n"], "realVolume24h": _f(trades["volume"]), "houseResult24h": _f(trades["house"]),
        "realTraders24h": trades["traders"],
        "openRealContracts": open_real["n"], "openRealAtRisk": _f(open_real["at_risk"]),
        "methodsWaiting": methods["n"],
        "marketsLive": sum(1 for v in snap.values() if v["live"]), "markets": len(prices.MARKETS),
    }


# ------------------------------------------------------------------- users --
@router.get("/users")
async def users(q: str = "", limit: int = Query(default=50, ge=1, le=200), _: str = Depends(admin)):
    like = f"%{q.strip().lower()}%"
    digits = "".join(ch for ch in q if ch.isdigit())
    rows = await db.many(
        """select p.id, p.email, p.full_name, p.phone, p.country_name, p.created_at,
                  coalesce(max(a.balance) filter (where a.kind = 'real'), 0) as real_balance,
                  coalesce(max(a.balance) filter (where a.kind = 'demo'), 0) as demo_balance
             from app.profiles p
             left join app.accounts a on a.user_id = p.id
            where %s = '' or lower(p.email) like %s or lower(coalesce(p.full_name, '')) like %s
                  or (%s <> '' and regexp_replace(coalesce(p.phone, ''), '\\D', '', 'g') like %s)
                  or p.referral_code = upper(%s)
            group by p.id
            order by p.created_at desc
            limit %s""",
        (q.strip(), like, like, digits, f"%{digits[-9:]}%", q.strip(), limit))
    return [{"id": str(r["id"]), "email": r["email"], "name": r["full_name"] or "", "phone": r["phone"] or "",
             "country": r["country_name"], "createdAt": _iso(r["created_at"]),
             "real": _f(r["real_balance"]), "demo": _f(r["demo_balance"])} for r in rows]


@router.get("/users/{user_id}")
async def user(user_id: str, _: str = Depends(admin)):
    try:
        p = await db.one("select * from app.profiles where id = %s", (user_id,))
    except Exception:
        p = None
    if not p:
        raise HTTPException(status_code=404, detail="No such user.")
    ref = await db.one("select email from app.profiles where id = %s", (p["referred_by"],)) if p["referred_by"] else None
    accounts = await db.many("select kind, balance from app.accounts where user_id = %s order by kind", (user_id,))
    txs = await db.many(
        """select reference, type, method, status, amount_usd, fee_usd, net_usd, local_amount, local_currency,
                  destination, provider_receipt, failure_reason, created_at, completed_at
             from app.transactions where user_id = %s order by created_at desc limit 50""", (user_id,))
    trades = await db.many(
        """select ref, account_kind, symbol, direction, stake, payout_pct, entry_price, exit_price, status,
                  profit, opened_at, settled_at
             from app.trades where user_id = %s order by opened_at desc limit 50""", (user_id,))
    methods = await db.many(
        """select id, kind, label, masked, status, is_default, created_at, removed_at
             from app.payment_methods where user_id = %s order by created_at desc""", (user_id,))
    summary = await db.one(
        """select coalesce(sum(net_usd) filter (where type = 'deposit' and status = 'completed'), 0) as deposited,
                  coalesce(sum(amount_usd) filter (where type = 'withdrawal' and status = 'completed'), 0) as withdrawn
             from app.transactions where user_id = %s""", (user_id,))
    real_pl = await db.one(
        "select coalesce(sum(profit), 0) as pl from app.trades where user_id = %s and account_kind = 'real' and status <> 'open'",
        (user_id,))
    return {
        "id": str(p["id"]), "email": p["email"], "name": p["full_name"] or "", "phone": p["phone"] or "",
        "country": p["country_name"], "currency": p["currency"], "referralCode": p["referral_code"],
        "referredBy": ref["email"] if ref else None, "createdAt": _iso(p["created_at"]),
        "balances": {a["kind"]: _f(a["balance"]) for a in accounts},
        "deposited": _f(summary["deposited"]), "withdrawn": _f(summary["withdrawn"]), "realPl": _f(real_pl["pl"]),
        "transactions": [_tx(t) for t in txs],
        "trades": [_trade(t) for t in trades],
        "methods": [{"id": str(m["id"]), "kind": m["kind"], "label": m["label"], "masked": m["masked"],
                     "status": m["status"], "isDefault": m["is_default"], "createdAt": _iso(m["created_at"]),
                     "removed": m["removed_at"] is not None} for m in methods],
    }


def _tx(t: dict) -> dict:
    return {"reference": t["reference"], "type": t["type"], "method": t["method"], "status": t["status"],
            "amount": _f(t["amount_usd"]), "fee": _f(t["fee_usd"]), "net": _f(t["net_usd"]),
            "local": _f(t["local_amount"]) if t.get("local_amount") is not None else None,
            "localCurrency": t.get("local_currency"), "destination": t.get("destination"),
            "receipt": t.get("provider_receipt"), "failure": t.get("failure_reason"),
            "createdAt": _iso(t["created_at"]), "completedAt": _iso(t.get("completed_at")),
            **({"email": t["email"], "userId": str(t["user_id"])} if "email" in t else {})}


def _trade(t: dict) -> dict:
    return {"ref": t["ref"], "account": t["account_kind"], "symbol": t["symbol"], "direction": t["direction"],
            "stake": _f(t["stake"]), "payout": _f(t["payout_pct"]),
            "entry": format(t["entry_price"].normalize(), "f") if t["entry_price"] is not None else None,
            "exit": format(t["exit_price"].normalize(), "f") if t["exit_price"] is not None else None,
            "status": t["status"], "profit": _f(t["profit"]) if t["profit"] is not None else None,
            "openedAt": _iso(t["opened_at"]), "settledAt": _iso(t["settled_at"]),
            **({"email": t["email"], "userId": str(t["user_id"])} if "email" in t else {})}


# ------------------------------------------------------------ transactions --
@router.get("/transactions")
async def transactions(type: Literal["deposit", "withdrawal", "referral", "all"] = "all",
                       status: Literal["waiting", "pending", "completed", "failed", "all"] = "all",
                       limit: int = Query(default=100, ge=1, le=500), _: str = Depends(admin)):
    where, args = ["true"], []
    if type != "all":
        where.append("t.type = %s"); args.append(type)
    if status == "waiting":
        where.append("t.status in ('review', 'processing')")
    elif status == "failed":
        where.append("t.status in ('failed', 'cancelled')")
    elif status != "all":
        where.append("t.status = %s"); args.append(status)
    rows = await db.many(
        f"""select t.*, p.email from app.transactions t join app.profiles p on p.id = t.user_id
             where {' and '.join(where)} order by t.created_at {'asc' if status == 'waiting' else 'desc'} limit %s""",
        (*args, limit))
    return [_tx(t) for t in rows]


class Approve(BaseModel):
    receipt: str = Field(min_length=3, max_length=120)   # the M-Pesa receipt, bank reference or tx hash you paid with


class Reject(BaseModel):
    reason: str = Field(min_length=3, max_length=300)    # shown to the user


@router.post("/withdrawals/{ref}/approve")
async def approve(ref: str, body: Approve, ip: str = Depends(admin)):
    async with db.tx() as conn:
        cur = await conn.execute(
            """update app.transactions set status = 'completed', completed_at = now(), provider_receipt = %s
                where reference = %s and type = 'withdrawal' and status in ('review', 'processing')
            returning *""", (body.receipt.strip(), ref))
        t = await cur.fetchone()
        if not t:
            raise HTTPException(status_code=409, detail="That withdrawal is not waiting any more.")
        await _log("withdrawal.approve", ref, t["user_id"], body.receipt.strip(), ip, conn)
    notify._later(notify.withdrawal_paid(t))
    return _tx(t)


@router.post("/withdrawals/{ref}/reject")
async def reject(ref: str, body: Reject, ip: str = Depends(admin)):
    async with db.tx() as conn:
        cur = await conn.execute(
            """update app.transactions set status = 'failed', failure_reason = %s, completed_at = now()
                where reference = %s and type = 'withdrawal' and status in ('review', 'processing')
            returning *""", (body.reason.strip(), ref))
        t = await cur.fetchone()
        if not t:
            raise HTTPException(status_code=409, detail="That withdrawal is not waiting any more.")
        await conn.execute("update app.accounts set balance = balance + %s where id = %s",
                           (t["net_usd"], t["account_id"]))
        await _log("withdrawal.reject", ref, t["user_id"], body.reason.strip(), ip, conn)
    notify._later(notify.withdrawal_rejected(t))
    return _tx(t)


# --------------------------------------------------------- payment methods --
@router.get("/payment-methods")
async def payment_methods(status: Literal["pending", "verified", "disabled", "all"] = "pending",
                          _: str = Depends(admin)):
    rows = await db.many(
        f"""select pm.*, p.email, p.full_name from app.payment_methods pm join app.profiles p on p.id = pm.user_id
             where pm.removed_at is null {"and pm.status = %s" if status != "all" else ""}
             order by pm.created_at {'asc' if status == 'pending' else 'desc'} limit 300""",
        (status,) if status != "all" else ())
    return [{"id": str(m["id"]), "userId": str(m["user_id"]), "email": m["email"], "name": m["full_name"] or "",
             "kind": m["kind"], "label": m["label"], "masked": m["masked"], "details": m["details"],
             "status": m["status"], "createdAt": _iso(m["created_at"])} for m in rows]


class MethodChange(BaseModel):
    note: str | None = Field(default=None, max_length=300)


@router.post("/payment-methods/{method_id}/{action}")
async def change_method(method_id: str, action: Literal["verify", "block"], body: MethodChange,
                        ip: str = Depends(admin)):
    sql = ("update app.payment_methods set status = 'verified' where id = %s and status <> 'verified' returning *"
           if action == "verify" else
           "update app.payment_methods set status = 'disabled', is_default = false where id = %s returning *")
    async with db.tx() as conn:
        try:
            cur = await conn.execute(sql, (method_id,))
        except Exception:
            raise HTTPException(status_code=404, detail="No such payment method.")
        m = await cur.fetchone()
        if not m:
            raise HTTPException(status_code=409, detail="That method is already verified.")
        await _log(f"method.{action}", method_id, m["user_id"], body.note, ip, conn)
    return {"id": str(m["id"]), "status": m["status"]}


# ------------------------------------------------------------------ trades --
@router.get("/trades")
async def trades(account: Literal["real", "demo", "all"] = "real", status: Literal["open", "settled", "all"] = "all",
                 limit: int = Query(default=100, ge=1, le=500), _: str = Depends(admin)):
    where, args = ["true"], []
    if account != "all":
        where.append("t.account_kind = %s"); args.append(account)
    if status == "open":
        where.append("t.status = 'open'")
    elif status == "settled":
        where.append("t.status <> 'open'")
    rows = await db.many(
        f"""select t.*, p.email from app.trades t join app.profiles p on p.id = t.user_id
             where {' and '.join(where)} order by t.opened_at desc limit %s""", (*args, limit))
    return [_trade(t) for t in rows]


# -------------------------------------------------------------------- logs --
# app.events (services/events.py) for payments, callbacks, sign-ups and feeds;
# app.notifications for emails and SMS, read here as kinds "email" and "sms".
_NOTES = """select 'notify.' || n.channel as kind, n.status = 'sent' as ok, n.channel as provider,
                   n.kind as ref, n.user_id, n.to_address, n.subject,
                   case when n.status = 'sent' then n.kind || ' to ' || n.to_address
                        else n.status || ': ' || coalesce(n.error, n.kind) end as detail,
                   null::integer as ms, n.created_at, p.email
              from app.notifications n left join app.profiles p on p.id = n.user_id"""


@router.get("/logs/summary")
async def logs_summary(hours: int = Query(default=24, ge=1, le=24 * 30), _: str = Depends(admin)):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = await db.many(
        """select kind, count(*) as total, count(*) filter (where not ok) as failed,
                  max(created_at) as last_at, max(created_at) filter (where not ok) as last_failed_at,
                  round(avg(ms) filter (where ms is not null)) as avg_ms
             from app.events where created_at >= %s group by kind""", (since,))
    notes = await db.many(
        """select 'notify.' || channel as kind, count(*) as total, count(*) filter (where status = 'failed') as failed,
                  max(created_at) as last_at, max(created_at) filter (where status = 'failed') as last_failed_at,
                  null as avg_ms
             from app.notifications where created_at >= %s group by channel""", (since,))
    last = await db.many(
        """select distinct on (kind) kind, detail from app.events
            where not ok and created_at >= %s order by kind, created_at desc""", (since,))
    last_note = await db.many(
        """select distinct on (channel) 'notify.' || channel as kind, coalesce(error, kind) as detail
             from app.notifications where status = 'failed' and created_at >= %s
            order by channel, created_at desc""", (since,))
    why = {r["kind"]: r["detail"] for r in [*last, *last_note]}
    return [{"kind": r["kind"], "total": r["total"], "failed": r["failed"], "lastAt": _iso(r["last_at"]),
             "lastFailedAt": _iso(r["last_failed_at"]), "lastError": why.get(r["kind"]),
             "avgMs": int(r["avg_ms"]) if r["avg_ms"] is not None else None} for r in [*rows, *notes]]


@router.get("/logs")
async def logs(kind: str = "", failed: bool = False, limit: int = Query(default=200, ge=1, le=500),
               _: str = Depends(admin)):
    """The latest events, newest first; `kind` a full kind (mpesa.stk) or a group (mpesa, webhook, notify)."""
    where, args = ["true"], []
    if kind:
        where.append("(x.kind = %s or x.kind like %s)"); args += [kind, kind + ".%"]
    if failed:
        where.append("not x.ok")
    rows = await db.many(
        f"""select * from (
              select e.kind, e.ok, e.provider, e.ref, e.user_id, e.detail, e.ms, e.created_at, p.email
                from app.events e left join app.profiles p on p.id = e.user_id
              union all
              select kind, ok, provider, ref, user_id, detail, ms, created_at, email from ({_NOTES}) n
            ) x where {' and '.join(where)} order by x.created_at desc limit %s""", (*args, limit))
    return [{"kind": r["kind"], "ok": r["ok"], "provider": r["provider"], "ref": r["ref"],
             "userId": str(r["user_id"]) if r["user_id"] else None, "email": r["email"], "detail": r["detail"],
             "ms": r["ms"], "at": _iso(r["created_at"])} for r in rows]


# ------------------------------------------------------------------ system --
@router.get("/actions")
async def actions(limit: int = Query(default=100, ge=1, le=500), _: str = Depends(admin)):
    rows = await db.many(
        """select a.*, p.email from app.admin_actions a left join app.profiles p on p.id = a.user_id
            order by a.created_at desc limit %s""", (limit,))
    return [{"id": r["id"], "action": r["action"], "target": r["target"], "email": r["email"], "note": r["note"],
             "userId": str(r["user_id"]) if r["user_id"] else None,
             "ip": r["ip"], "at": _iso(r["created_at"])} for r in rows]


@router.get("/system")
async def system(_: str = Depends(admin)):
    s = get_settings()
    snap = prices.snapshot()
    now = time.time()
    markets = [{"sym": m.sym, "source": m.source, "payout": m.payout,
                "price": snap[m.sym]["p"] if m.sym in snap else None,
                "age": round(now - snap[m.sym]["t"], 1) if m.sym in snap else None,
                "live": bool(snap.get(m.sym, {}).get("live"))} for m in prices.MARKETS.values()]
    return {
        "frontendUrl": s.frontend_url, "adminUrl": s.admin_url,
        "minDepositUsd": s.min_deposit_usd, "minWithdrawUsd": s.min_withdraw_usd, "withdrawFeeUsd": s.withdraw_fee_usd,
        "usdKesRate": s.usd_kes_rate,
        "providers": {
            "Kora (M-Pesa)": bool(s.kora_secret_key),
            "PayHero (M-Pesa)": bool(s.payhero_api_username and s.payhero_api_password),
            "Paystack (card)": bool(s.paystack_secret_key),
            "Resend (email)": bool(s.resend_api_key),
            "Africa's Talking (SMS)": bool(s.at_username and s.at_api_key),
            "Finnhub (forex prices, news)": bool(s.finnhub_api_key),
        },
        "markets": markets,
    }
