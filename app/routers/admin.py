"""The admin API, for the admin site (a separate app, see its README).

It exists only when ADMIN_TOKEN is set (32+ characters); without it every
path here answers 404. The site sends the token as a bearer token. Wrong
tokens are limited to 10 per 15 minutes per address.

Reads cover users, money, trades and the price feed. The few writes are the
ones that used to be SQL run by hand (sql/admin): approving or rejecting a
withdrawal, verifying or blocking a payment method, and VIP accounts (making
one, crediting or debiting its real balance). Each is recorded in
app.admin_actions (sql/009_admin.sql); withdrawal outcomes are emailed.

VIP accounts (sql/012_vip.sql) are demonstration accounts. Their money moves
by M-Pesa through a demo wallet (sql/013_vip_mpesa.sql) that the M-Pesa clone
app shows, set up here (PIN, balance, Fuliza limit, phone), or is credited
here directly. The totals on the overview leave them out.
"""
import base64
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from psycopg.errors import UndefinedColumn, UndefinedTable, UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..services import kora, mpesa_demo, notify, paystack, prices, sms, verification
from ..util import kenyan_msisdn, reference, usd

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

_misses: dict[str, deque] = defaultdict(deque)
MISS_LIMIT, MISS_WINDOW = 10, 15 * 60


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "")


class Principal(str):
    """Who is calling: the caller's IP (it is a str, for the audit log), plus
    the admin account behind the session. ADMIN_TOKEN itself is a super admin."""
    id: str | None
    email: str
    name: str
    role: str

    @classmethod
    def make(cls, ip: str, id: str | None, email: str, name: str, role: str) -> "Principal":
        p = cls(ip)
        p.id, p.email, p.name, p.role = id, email, name, role
        return p


ROLES = ("superadmin", "admin", "finance", "support", "marketer")

# a marketer sees only their own live sessions (sql/017); everything else answers 403
MARKETER_PATHS = ("/admin/me", "/admin/auth/", "/admin/live")


def need(who: Principal, *roles: str) -> None:
    """Refuse a change the caller's role does not cover. A super admin can do everything."""
    if who.role != "superadmin" and who.role not in roles:
        raise HTTPException(status_code=403, detail="Your admin role does not allow this. Ask a super admin.")


def _missed(ip: str) -> None:
    misses = _misses[ip]
    while misses and misses[0] < time.monotonic() - MISS_WINDOW:
        misses.popleft()
    if len(misses) >= MISS_LIMIT:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 minutes.")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def admin(request: Request) -> Principal:
    token = get_settings().admin_token
    if len(token) < 32:
        raise HTTPException(status_code=404, detail="Not Found")
    ip = _ip(request)
    _missed(ip)
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if given and hmac.compare_digest(given.encode(), token.encode()):
        return Principal.make(ip, None, "ADMIN_TOKEN", "Admin token", "superadmin")
    if given:
        try:
            row = await db.one(
                """select u.id, u.email, u.name, u.role from app.admin_sessions s
                     join app.admin_users u on u.id = s.admin_id
                    where s.token_hash = %s and s.expires_at > now() and u.active""", (_hash(given),))
        except Exception:                               # sql/016 not run, or the database is down
            row = None
        if row:
            if row["role"] == "marketer" and not request.url.path.startswith(MARKETER_PATHS):
                raise HTTPException(status_code=403, detail="Marketer accounts only see their live sessions.")
            return Principal.make(ip, str(row["id"]), row["email"], row["name"], row["role"])
    _misses[ip].append(time.monotonic())
    raise HTTPException(status_code=401, detail="Your session has ended. Sign in again.")


@asynccontextmanager
async def _nullctx():
    yield


def _f(v) -> float:
    return float(v or 0)


def _iso(v) -> str | None:
    return v.isoformat() if v else None


NAIROBI = ZoneInfo("Africa/Nairobi")


def _midnight() -> datetime:
    """The start of today in Nairobi, which is what "today" means on the admin site."""
    return datetime.now(NAIROBI).replace(hour=0, minute=0, second=0, microsecond=0)


def _range(since: datetime | None, until: datetime | None) -> tuple[datetime | None, datetime | None]:
    if since and until and since > until:
        raise HTTPException(status_code=400, detail="The start of the range is after its end.")
    return since, until


async def _log(action: str, target: str, user_id, note: str | None, ip: str, conn=None) -> None:
    actor = getattr(ip, "email", None)
    try:
        async with (conn.transaction() if conn is not None else _nullctx()):
            await db.run("insert into app.admin_actions (action, target, user_id, note, ip, actor) values (%s, %s, %s, %s, %s, %s)",
                         (action, target, user_id, note, str(ip), actor), conn=conn)
        return
    except UndefinedColumn:                             # sql/016 not run yet
        pass
    await db.run("insert into app.admin_actions (action, target, user_id, note, ip) values (%s, %s, %s, %s, %s)",
                 (action, target, user_id, note, str(ip)), conn=conn)


# ---------------------------------------------------------------- overview --
@router.get("/overview")
async def overview(_: str = Depends(admin)):
    day = datetime.now(timezone.utc) - timedelta(hours=24)
    week = datetime.now(timezone.utc) - timedelta(days=7)
    users = await db.one(
        """select count(*) as n, count(*) filter (where created_at >= %s) as new,
                  count(*) filter (where is_vip) as vip from app.profiles""", (day,))
    money = await db.one(
        """select coalesce(sum(a.balance) filter (where a.kind = 'real'), 0) as real_balances
             from app.accounts a join app.profiles p on p.id = a.user_id where not p.is_vip""")
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
             from app.trades t where account_kind = 'real' and status <> 'open' and settled_at >= %s
              and not exists (select 1 from app.profiles p where p.id = t.user_id and p.is_vip)""", (day,))
    open_real = await db.one(
        """select count(*) as n, coalesce(sum(stake), 0) as at_risk from app.trades t
            where account_kind = 'real' and status = 'open'
              and not exists (select 1 from app.profiles p where p.id = t.user_id and p.is_vip)""")
    methods = await db.one(
        "select count(*) as n from app.payment_methods where status = 'pending' and removed_at is null")
    snap = prices.snapshot()
    today = await db.one(
        """select coalesce(sum(t.net_usd) filter (where t.type = 'deposit'), 0) as dep,
                  count(*) filter (where t.type = 'deposit') as dep_n,
                  coalesce(sum(t.amount_usd) filter (where t.type = 'withdrawal'), 0) as wd,
                  count(*) filter (where t.type = 'withdrawal') as wd_n
             from app.transactions t join app.profiles p on p.id = t.user_id
            where t.status = 'completed' and not p.is_vip and t.completed_at >= %s""", (_midnight(),))
    try:
        kyc = (await db.one("select count(*) as n from app.verifications where status = 'pending'"))["n"]
    except UndefinedTable:
        kyc = None
    return {
        "usdKesRate": get_settings().usd_kes_rate,
        "depositsToday": _f(today["dep"]), "depositsTodayCount": today["dep_n"],
        "withdrawalsToday": _f(today["wd"]), "withdrawalsTodayCount": today["wd_n"],
        "verificationsWaiting": kyc,
        "users": users["n"], "newUsers24h": users["new"], "vipUsers": users["vip"],
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
async def users(q: str = "", vip: bool = False, limit: int = Query(default=50, ge=1, le=200), _: str = Depends(admin)):
    like = f"%{q.strip().lower()}%"
    digits = "".join(ch for ch in q if ch.isdigit())
    rows = await db.many(
        """select p.id, p.email, p.full_name, p.phone, p.country_name, p.created_at, p.is_vip,
                  coalesce(max(a.balance) filter (where a.kind = 'real'), 0) as real_balance,
                  coalesce(max(a.balance) filter (where a.kind = 'demo'), 0) as demo_balance
             from app.profiles p
             left join app.accounts a on a.user_id = p.id
            where (%s = '' or lower(p.email) like %s or lower(coalesce(p.full_name, '')) like %s
                  or (%s <> '' and regexp_replace(coalesce(p.phone, ''), '\\D', '', 'g') like %s)
                  or p.referral_code = upper(%s))
              and (not %s or p.is_vip)
            group by p.id
            order by p.created_at desc
            limit %s""",
        (q.strip(), like, like, digits, f"%{digits[-9:]}%", q.strip(), vip, limit))
    return [{"id": str(r["id"]), "email": r["email"], "name": r["full_name"] or "", "phone": r["phone"] or "",
             "country": r["country_name"], "createdAt": _iso(r["created_at"]),
             "real": _f(r["real_balance"]), "demo": _f(r["demo_balance"]), "vip": r["is_vip"]} for r in rows]


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
    kyc = await verification.state(user_id)
    return {
        "verification": {"ready": kyc["ready"], "verified": kyc["verified"], "identity": kyc["identity"],
                         "address": kyc["address"]},
        "id": str(p["id"]), "email": p["email"], "name": p["full_name"] or "", "phone": p["phone"] or "",
        "country": p["country_name"], "currency": p["currency"], "referralCode": p["referral_code"],
        "referredBy": ref["email"] if ref else None, "createdAt": _iso(p["created_at"]),
        "vip": bool(p.get("is_vip")), "vipSince": _iso(p.get("vip_since")),
        "staged": bool(p.get("staged_results")),
        "balances": {a["kind"]: _f(a["balance"]) for a in accounts},
        "deposited": _f(summary["deposited"]), "withdrawn": _f(summary["withdrawn"]), "realPl": _f(real_pl["pl"]),
        "transactions": [_tx(t) for t in txs],
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
            **({"email": t["email"], "userId": str(t["user_id"])} if "email" in t else {}),
            **({"vip": t["is_vip"]} if "is_vip" in t else {})}


# ------------------------------------------------------------ transactions --
TxType = Literal["deposit", "withdrawal", "referral", "credit", "debit", "all"]
TxStatus = Literal["waiting", "pending", "completed", "failed", "all"]
TxMethod = Literal["mpesa", "card", "bank", "usdt", "referral", "admin", "all"]


def _tx_where(type: str, status: str, method: str, since, until, vip: bool) -> tuple[str, list]:
    where, args = ["true"], []
    if type != "all":
        where.append("t.type = %s"); args.append(type)
    if status == "waiting":
        where.append("t.status in ('review', 'processing')")
    elif status == "failed":
        where.append("t.status in ('failed', 'cancelled')")
    elif status != "all":
        where.append("t.status = %s"); args.append(status)
    if method != "all":
        where.append("t.method = %s"); args.append(method)
    if since:
        where.append("t.created_at >= %s"); args.append(since)
    if until:
        where.append("t.created_at < %s"); args.append(until)
    if not vip:
        where.append("not p.is_vip")
    return " and ".join(where), args


@router.get("/transactions")
async def transactions(type: TxType = "all", status: TxStatus = "all", method: TxMethod = "all",
                       since: datetime | None = None, until: datetime | None = None, vip: bool = True,
                       limit: int = Query(default=100, ge=1, le=500), _: str = Depends(admin)):
    """`since` and `until` bound created_at (until exclusive). `vip=false` leaves VIP accounts out."""
    since, until = _range(since, until)
    where, args = _tx_where(type, status, method, since, until, vip)
    rows = await db.many(
        f"""select t.*, p.email, p.is_vip from app.transactions t join app.profiles p on p.id = t.user_id
             where {where} order by t.created_at {'asc' if status == 'waiting' else 'desc'} limit %s""",
        (*args, limit))
    return [_tx(t) for t in rows]


@router.get("/transactions/totals")
async def transaction_totals(type: TxType = "all", status: TxStatus = "all", method: TxMethod = "all",
                             since: datetime | None = None, until: datetime | None = None, vip: bool = True,
                             _: str = Depends(admin)):
    """What the same filters add up to, whatever the row limit: by status, so a
    deposits page can show what came in next to what is still pending."""
    since, until = _range(since, until)
    where, args = _tx_where(type, status, method, since, until, vip)
    rows = await db.many(
        f"""select case when t.status in ('review', 'processing') then 'waiting'
                        when t.status in ('failed', 'cancelled') then 'failed' else t.status end as s,
                   count(*) as n, coalesce(sum(t.amount_usd), 0) as amount, coalesce(sum(t.net_usd), 0) as net,
                   coalesce(sum(t.fee_usd), 0) as fee,
                   coalesce(sum(t.local_amount) filter (where t.local_currency = 'KES'), 0) as kes,
                   count(distinct t.user_id) as users
              from app.transactions t join app.profiles p on p.id = t.user_id
             where {where} group by 1""", args)
    out = {r["s"]: {"count": r["n"], "amount": _f(r["amount"]), "net": _f(r["net"]), "fee": _f(r["fee"]),
                    "kes": _f(r["kes"]), "users": r["users"]} for r in rows}
    return {"byStatus": out, "count": sum(r["n"] for r in rows)}


class Approve(BaseModel):
    receipt: str = Field(min_length=3, max_length=120)   # the M-Pesa receipt, bank reference or tx hash you paid with


class Reject(BaseModel):
    reason: str = Field(min_length=3, max_length=300)    # shown to the user


@router.post("/withdrawals/{ref}/approve")
async def approve(ref: str, body: Approve, ip: Principal = Depends(admin)):
    need(ip, "finance")
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
async def reject(ref: str, body: Reject, ip: Principal = Depends(admin)):
    need(ip, "finance")
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


# --------------------------------------------------------------------- vip --
class VipChange(BaseModel):
    vip: bool
    note: str | None = Field(default=None, max_length=300)


class BalanceChange(BaseModel):
    action: Literal["credit", "debit"]
    amount_usd: float = Field(gt=0, le=1_000_000)
    note: str | None = Field(default=None, max_length=300)


async def _real(conn, user_id: str) -> dict:
    """The user's VIP flag and real account, locked for the change."""
    try:
        cur = await conn.execute(
            """select p.is_vip, a.id as account_id, a.balance from app.profiles p
                 join app.accounts a on a.user_id = p.id and a.kind = 'real'
                where p.id = %s for update of p, a""", (user_id,))
    except Exception:                                   # not a uuid
        raise HTTPException(status_code=404, detail="No such user.")
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No such user.")
    return row


async def _move(conn, user_id: str, account_id, type: str, amount: Decimal, note: str | None) -> dict:
    """A completed credit or debit, so the user's history shows where the money came from."""
    cur = await conn.execute(
        """insert into app.transactions (user_id, account_id, type, method, provider, status, amount_usd, fee_usd,
                                         net_usd, reference, destination, meta, completed_at)
           values (%s, %s, %s, 'admin', 'admin', 'completed', %s, 0, %s, %s, 'VIP account', %s, now())
           returning *""",
        (user_id, account_id, type, amount, amount, reference("CR" if type == "credit" else "DB"),
         Jsonb({"note": note} if note else {})))
    return await cur.fetchone()


@router.post("/users/{user_id}/vip")
async def set_vip(user_id: str, body: VipChange, ip: Principal = Depends(admin)):
    """Make a user a VIP, or a VIP an ordinary user again. A user becomes a VIP
    only with nothing in the real account, so no real money is ever locked in
    one; a VIP stops being one with its credited balance taken back to zero."""
    need(ip, "admin")
    note = (body.note or "").strip() or None
    async with db.tx() as conn:
        row = await _real(conn, user_id)
        if row["is_vip"] == body.vip:
            raise HTTPException(status_code=409, detail="Already a VIP." if body.vip else "Not a VIP.")
        cur = await conn.execute(
            "select 1 from app.trades where user_id = %s and account_kind = 'real' and status = 'open' limit 1",
            (user_id,))
        if await cur.fetchone():
            raise HTTPException(status_code=409, detail="Wait for this user's open real contracts to finish first.")
        if body.vip:
            if row["balance"] > 0:
                raise HTTPException(status_code=409, detail=(
                    f"This user has ${row['balance']:,.2f} of their own money. A VIP's real balance is only "
                    "ever money credited here, so make VIPs from accounts with nothing in them."))
            # an unfinished deposit would credit their own money into a VIP account: it goes. A
            # withdrawal in review stays, for you to pay or reject as usual.
            await conn.execute(
                """delete from app.transactions where user_id = %s and type = 'deposit'
                      and status in ('pending', 'processing')""", (user_id,))
        elif row["balance"] > 0:
            await _move(conn, user_id, row["account_id"], "debit", row["balance"], "VIP removed")
            await conn.execute("update app.accounts set balance = 0 where id = %s", (row["account_id"],))
        await conn.execute(
            "update app.profiles set is_vip = %s, vip_since = case when %s then now() end where id = %s",
            (body.vip, body.vip, user_id))
        if not body.vip:                                # staged results go with VIP (sql/014)
            try:
                async with conn.transaction():
                    await conn.execute("update app.profiles set staged_results = false where id = %s", (user_id,))
            except Exception:                           # sql/014 not run yet
                pass
        await _log("vip.grant" if body.vip else "vip.remove", user_id, user_id, note, ip, conn)
    return {"id": user_id, "vip": body.vip}


@router.post("/users/{user_id}/balance")
async def change_balance(user_id: str, body: BalanceChange, ip: Principal = Depends(admin)):
    """Credit or debit a VIP's real balance. Ordinary users' balances are never
    touched from here: their money only moves through deposits and withdrawals."""
    need(ip, "admin")
    amount = usd(body.amount_usd)
    note = (body.note or "").strip() or None
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Enter an amount of at least $0.01.")
    async with db.tx() as conn:
        row = await _real(conn, user_id)
        if not row["is_vip"]:
            raise HTTPException(status_code=403, detail="Only VIP accounts can be credited. Make this user a VIP first.")
        if body.action == "debit" and amount > row["balance"]:
            raise HTTPException(status_code=400, detail=f"The real balance is only ${row['balance']:,.2f}.")
        t = await _move(conn, user_id, row["account_id"], body.action, amount, note)
        cur = await conn.execute("update app.accounts set balance = balance + %s where id = %s returning balance",
                                 (amount if body.action == "credit" else -amount, row["account_id"]))
        balance = (await cur.fetchone())["balance"]
        await _log(f"vip.{body.action}", t["reference"], user_id, f"${amount:,.2f}" + (f": {note}" if note else ""),
                   ip, conn)
    notify._later(notify.vip_balance(t, balance))
    return {"transaction": _tx(t), "real": _f(balance)}


class StagedChange(BaseModel):
    on: bool


@router.post("/users/{user_id}/staged")
async def set_staged(user_id: str, body: StagedChange, ip: Principal = Depends(admin)):
    """Staged results for a VIP (sql/014): its contracts are settled in its
    favour STAGED_WIN_PCT of the time. Each contract is marked staged."""
    need(ip, "admin")
    p = await _profile(user_id)
    if body.on and not p["is_vip"]:
        raise HTTPException(status_code=403, detail="Staged results are only for VIP accounts. Make this user a VIP first.")
    try:
        await db.run("update app.profiles set staged_results = %s where id = %s", (body.on, user_id))
    except Exception:
        raise HTTPException(status_code=503, detail="Run sql/014_staged_results.sql first.")
    await _log("staged.on" if body.on else "staged.off", user_id, user_id, None, ip)
    return {"id": user_id, "staged": body.on}


# ------------------------------------------------------- vip demo wallet --
class WalletChange(BaseModel):
    pin: str | None = Field(default=None, pattern=r"^\d{4}$")
    balance_kes: float | None = Field(default=None, ge=0, le=10_000_000)
    fuliza_limit_kes: float | None = Field(default=None, ge=0, le=10_000_000)
    name: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=20)


class WalletReset(BaseModel):
    balance_kes: float | None = Field(default=None, ge=0, le=10_000_000)


class TestSms(BaseModel):
    phone: str | None = Field(default=None, max_length=20)


async def _profile(user_id: str) -> dict:
    try:
        p = await db.one("select id, email, full_name, phone, is_vip from app.profiles where id = %s", (user_id,))
    except Exception:                                   # not a uuid
        p = None
    if not p:
        raise HTTPException(status_code=404, detail="No such user.")
    return p


def _gateway() -> dict:
    return {"provider": sms.provider(), "sender": sms.sender()}


@router.get("/users/{user_id}/wallet")
async def wallet(user_id: str, _: str = Depends(admin)):
    """The VIP's M-Pesa demo wallet, its last 20 movements, and which SMS
    gateway its texts go through. The PIN is returned whole: it is what the
    tester types into the clone app, and it guards nothing real."""
    await _profile(user_id)
    w = await mpesa_demo.wallet_for(user_id)
    return {"wallet": w, "statement": await mpesa_demo.statement(user_id, 20) if w else [], "sms": _gateway()}


@router.post("/users/{user_id}/wallet")
async def set_wallet(user_id: str, body: WalletChange, ip: Principal = Depends(admin)):
    """Create the demo wallet (a PIN is needed then) or change any part of it.
    The name and phone default to the profile's."""
    need(ip, "admin")
    p = await _profile(user_id)
    if not p["is_vip"]:
        raise HTTPException(status_code=403, detail="Only VIP accounts have a demo wallet. Make this user a VIP first.")
    phone = None
    if body.phone is not None and body.phone.strip():
        phone = kenyan_msisdn(body.phone)
        if not phone:
            raise HTTPException(status_code=400, detail="Enter a Kenyan mobile number, e.g. 0712 345 678.")
    elif not await mpesa_demo.wallet_for(user_id):
        phone = kenyan_msisdn(p["phone"] or "")
    w = await mpesa_demo.set_wallet(
        user_id, pin=body.pin,
        balance_minor=None if body.balance_kes is None else mpesa_demo.kes_minor(body.balance_kes),
        fuliza_limit_minor=None if body.fuliza_limit_kes is None else mpesa_demo.kes_minor(body.fuliza_limit_kes),
        name=(body.name or "").strip() or p["full_name"] or None, phone=phone)
    # the PIN itself never goes in the log: a log of PINs is a list of keys to every wallet
    bits = [k for k, v in (("PIN", body.pin), ("balance", body.balance_kes), ("Fuliza limit", body.fuliza_limit_kes),
                           ("phone", phone)) if v is not None]
    await _log("wallet.set", user_id, user_id, "changed " + (", ".join(bits) or "name"), ip)
    return {"wallet": w, "statement": await mpesa_demo.statement(user_id, 20), "sms": _gateway()}


@router.post("/users/{user_id}/wallet/reset")
async def reset_wallet(user_id: str, body: WalletReset, ip: Principal = Depends(admin)):
    """Between rehearsals: an empty statement and nothing owed, at the balance
    given (or the current one). The handset stays linked."""
    need(ip, "admin")
    await _profile(user_id)
    w = await mpesa_demo.reset(user_id, None if body.balance_kes is None else mpesa_demo.kes_minor(body.balance_kes))
    await _log("wallet.reset", user_id, user_id, None if body.balance_kes is None else f"KSh {body.balance_kes:,.2f}", ip)
    return {"wallet": w, "statement": [], "sms": _gateway()}


@router.post("/users/{user_id}/wallet/test-sms")
async def test_sms(user_id: str, body: TestSms, ip: Principal = Depends(admin)):
    """One plainly worded text to the wallet's number, sent while you wait, so
    the gateway, the sender and the number are known to work before a demo."""
    need(ip, "admin")
    await _profile(user_id)
    if not get_settings().sms_ready:
        raise HTTPException(status_code=400, detail="No SMS gateway is configured. Set CELCOM_API_KEY, CELCOM_PARTNER_ID "
                                                    "and CELCOM_SHORTCODE, or AT_USERNAME and AT_API_KEY, on the API.")
    w = await mpesa_demo.wallet_for(user_id)
    if not w:
        raise HTTPException(status_code=404, detail="This account has no demo wallet yet.")
    to = (body.phone or "").strip() or w["phone"]
    if not to:
        raise HTTPException(status_code=400, detail="This wallet has no phone number. Add one first.")
    out = await sms.send(to, mpesa_demo.test_message(w["holderName"]))
    await _log("wallet.test_sms", user_id, user_id, f"{out['status']} via {out.get('provider')}: {out['detail']}"[:300], ip)
    return {"ok": out["ok"], "status": out["status"], "detail": out["detail"], "provider": out.get("provider"),
            "sender": sms.sender(), "to": to}


@router.post("/users/{user_id}/wallet/clear")
async def clear_wallet(user_id: str, ip: Principal = Depends(admin)):
    """Remove the wallet and its statement. A handset linked to it is unlinked."""
    need(ip, "admin")
    await _profile(user_id)
    await mpesa_demo.clear(user_id)
    await _log("wallet.clear", user_id, user_id, None, ip)
    return {"wallet": None, "statement": [], "sms": _gateway()}


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
                        ip: Principal = Depends(admin)):
    need(ip, "admin", "support")
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
             "ip": r["ip"], "actor": r.get("actor"), "at": _iso(r["created_at"])} for r in rows]


@router.get("/system")
async def system(_: str = Depends(admin)):
    s = get_settings()
    snap = prices.snapshot()
    now = time.time()
    markets = [{"sym": m.sym, "source": "obisflow", "payout": m.payout,
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
            "Finnhub (market news)": bool(s.finnhub_api_key),
        },
        "markets": markets,
    }


# ----------------------------------------------------------------- finance --
@router.get("/finance")
async def finance(since: datetime | None = None, until: datetime | None = None, _: str = Depends(admin)):
    """Real money over a range (default: the last 30 days), VIP accounts left
    out: what came in, what went out, what the house made on contracts, and
    the same per day (Nairobi days) for the chart. Balances are as of now."""
    now = datetime.now(NAIROBI)
    since = since or (_midnight() - timedelta(days=29))
    until = until or now
    since, until = _range(since, until)
    rng = (since, until)
    money = await db.one(
        """select
             coalesce(sum(t.net_usd)    filter (where t.type = 'deposit'), 0)    as dep,
             count(*)                   filter (where t.type = 'deposit')        as dep_n,
             count(distinct t.user_id)  filter (where t.type = 'deposit')        as depositors,
             coalesce(sum(t.fee_usd)    filter (where t.type = 'deposit'), 0)    as dep_fee,
             coalesce(sum(t.amount_usd) filter (where t.type = 'withdrawal'), 0) as wd,
             count(*)                   filter (where t.type = 'withdrawal')     as wd_n,
             coalesce(sum(t.fee_usd)    filter (where t.type = 'withdrawal'), 0) as wd_fee,
             coalesce(sum(t.net_usd)    filter (where t.type = 'referral'), 0)   as referral
           from app.transactions t join app.profiles p on p.id = t.user_id
          where t.status = 'completed' and not p.is_vip and t.completed_at >= %s and t.completed_at < %s""", rng)
    first = await db.one(
        """select count(*) as n from (
             select t.user_id, min(t.completed_at) as first_at from app.transactions t
               join app.profiles p on p.id = t.user_id
              where t.type = 'deposit' and t.status = 'completed' and not p.is_vip group by t.user_id) f
            where f.first_at >= %s and f.first_at < %s""", rng)
    trades = await db.one(
        """select count(*) as n, coalesce(sum(stake), 0) as volume, coalesce(-sum(profit), 0) as house,
                  count(distinct user_id) as traders
             from app.trades t where account_kind = 'real' and status <> 'open' and settled_at >= %s and settled_at < %s
              and not exists (select 1 from app.profiles p where p.id = t.user_id and p.is_vip)""", rng)
    held = await db.one(
        """select coalesce(sum(a.balance), 0) as real, count(*) filter (where a.balance > 0) as funded
             from app.accounts a join app.profiles p on p.id = a.user_id where a.kind = 'real' and not p.is_vip""")
    waiting = await db.one(
        """select count(*) as n, coalesce(sum(t.net_usd), 0) as usd from app.transactions t
             join app.profiles p on p.id = t.user_id
            where t.type = 'withdrawal' and t.status in ('review', 'processing') and not p.is_vip""")
    by_method = await db.many(
        """select t.type, t.method, count(*) as n, coalesce(sum(case when t.type = 'deposit' then t.net_usd else t.amount_usd end), 0) as usd
             from app.transactions t join app.profiles p on p.id = t.user_id
            where t.status = 'completed' and not p.is_vip and t.type in ('deposit', 'withdrawal')
              and t.completed_at >= %s and t.completed_at < %s
            group by 1, 2 order by usd desc""", rng)
    days = await db.many(
        """with d as (
             select generate_series(date_trunc('day', %s::timestamptz at time zone 'Africa/Nairobi'),
                                    date_trunc('day', %s::timestamptz at time zone 'Africa/Nairobi'),
                                    interval '1 day') as day),
           m as (
             select date_trunc('day', t.completed_at at time zone 'Africa/Nairobi') as day,
                    coalesce(sum(t.net_usd) filter (where t.type = 'deposit'), 0) as dep,
                    coalesce(sum(t.amount_usd) filter (where t.type = 'withdrawal'), 0) as wd
               from app.transactions t join app.profiles p on p.id = t.user_id
              where t.status = 'completed' and not p.is_vip and t.type in ('deposit', 'withdrawal')
                and t.completed_at >= %s and t.completed_at < %s group by 1),
           h as (
             select date_trunc('day', t.settled_at at time zone 'Africa/Nairobi') as day, coalesce(-sum(t.profit), 0) as house
               from app.trades t
              where t.account_kind = 'real' and t.status <> 'open' and t.settled_at >= %s and t.settled_at < %s
                and not exists (select 1 from app.profiles p where p.id = t.user_id and p.is_vip) group by 1)
           select d.day, coalesce(m.dep, 0) as dep, coalesce(m.wd, 0) as wd, coalesce(h.house, 0) as house
             from d left join m using (day) left join h using (day) order by d.day""",
        (since, until, since, until, since, until))
    top = await db.many(
        """select p.id, p.email, p.full_name, count(*) as n, sum(t.net_usd) as usd
             from app.transactions t join app.profiles p on p.id = t.user_id
            where t.type = 'deposit' and t.status = 'completed' and not p.is_vip
              and t.completed_at >= %s and t.completed_at < %s
            group by p.id order by usd desc limit 8""", rng)
    return {
        "since": since.isoformat(), "until": until.isoformat(), "usdKesRate": get_settings().usd_kes_rate,
        "deposits": _f(money["dep"]), "depositsCount": money["dep_n"], "depositors": money["depositors"],
        "firstDeposits": first["n"], "depositFees": _f(money["dep_fee"]),
        "withdrawals": _f(money["wd"]), "withdrawalsCount": money["wd_n"], "withdrawalFees": _f(money["wd_fee"]),
        "referralPaid": _f(money["referral"]),
        "net": _f(money["dep"]) - _f(money["wd"]),
        "houseResult": _f(trades["house"]), "volume": _f(trades["volume"]), "trades": trades["n"],
        "traders": trades["traders"],
        "realBalances": _f(held["real"]), "fundedAccounts": held["funded"],
        "withdrawalsWaiting": waiting["n"], "withdrawalsWaitingUsd": _f(waiting["usd"]),
        "byMethod": [{"type": r["type"], "method": r["method"], "count": r["n"], "usd": _f(r["usd"])} for r in by_method],
        "days": [{"day": r["day"].date().isoformat(), "deposits": _f(r["dep"]), "withdrawals": _f(r["wd"]),
                  "house": _f(r["house"])} for r in days],
        "topDepositors": [{"id": str(r["id"]), "email": r["email"], "name": r["full_name"] or "", "count": r["n"],
                           "usd": _f(r["usd"])} for r in top],
    }


# ----------------------------------------------------------- verification --
def _kyc_row(r: dict) -> dict:
    return {"id": str(r["id"]), "userId": str(r["user_id"]), "email": r["email"], "name": r["full_name"] or "",
            "phone": r["phone"] or "", "country": r["country_name"], "joined": _iso(r["joined"]),
            "kind": r["kind"], "docType": r["doc_type"], "docName": verification.DOC_NAME.get(r["doc_type"], r["doc_type"]),
            "status": r["status"], "reason": r["reason"], "createdAt": _iso(r["created_at"]),
            "decidedAt": _iso(r["decided_at"]), "vip": r["is_vip"],
            "files": [{"id": str(f["id"]), "side": f["side"], "mime": f["mime"], "size": f["size"]} for f in r.get("files", [])]}


_KYC = """select v.*, p.email, p.full_name, p.phone, p.country_name, p.created_at as joined, p.is_vip
            from app.verifications v join app.profiles p on p.id = v.user_id"""


@router.get("/verifications")
async def verifications(status: Literal["pending", "approved", "rejected", "all"] = "pending",
                        kind: Literal["identity", "address", "all"] = "all", _: str = Depends(admin)):
    where, args = ["true"], []
    if status != "all":
        where.append("v.status = %s"); args.append(status)
    if kind != "all":
        where.append("v.kind = %s"); args.append(kind)
    try:
        rows = await db.many(f"{_KYC} where {' and '.join(where)} order by v.created_at "
                             f"{'asc' if status == 'pending' else 'desc'} limit 300", args)
        files = await db.many(
            "select id, verification_id, side, mime, size from app.verification_files where verification_id = any(%s) "
            "order by created_at", ([r["id"] for r in rows],)) if rows else []
    except UndefinedTable:
        raise HTTPException(status_code=503, detail="Run sql/015_verification.sql on the database first.")
    for r in rows:
        r["files"] = [f for f in files if f["verification_id"] == r["id"]]
    return [_kyc_row(r) for r in rows]


@router.get("/verifications/{vid}/files/{fid}")
async def verification_file(vid: str, fid: str, _: str = Depends(admin)):
    """The file itself. The admin site fetches it with the token and shows it
    from memory; it is never cached or given a public address."""
    try:
        f = await db.one("select mime, data from app.verification_files where id = %s and verification_id = %s", (fid, vid))
    except Exception:
        f = None
    if not f:
        raise HTTPException(status_code=404, detail="No such file.")
    return Response(content=bytes(f["data"]), media_type=f["mime"],
                    headers={"cache-control": "no-store", "x-content-type-options": "nosniff"})


class KycDecision(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


@router.post("/verifications/{vid}/{action}")
async def decide_verification(vid: str, action: Literal["approve", "reject"], body: KycDecision, ip: Principal = Depends(admin)):
    need(ip, "admin", "support")
    reason = (body.reason or "").strip() or None
    if action == "reject" and (not reason or len(reason) < 3):
        raise HTTPException(status_code=400, detail="Say why, so the user knows what to send instead.")
    async with db.tx() as conn:
        try:
            cur = await conn.execute(
                """update app.verifications set status = %s, reason = %s, decided_at = now()
                    where id = %s and status = 'pending' returning *""",
                ("approved" if action == "approve" else "rejected", reason if action == "reject" else None, vid))
        except Exception:
            raise HTTPException(status_code=404, detail="No such submission.")
        v = await cur.fetchone()
        if not v:
            raise HTTPException(status_code=409, detail="That submission was already decided, or replaced by a newer one.")
        await _log(f"verification.{action}", str(vid), v["user_id"],
                   f"{verification.DOC_NAME.get(v['doc_type'], v['doc_type'])}" + (f": {reason}" if reason else ""), ip, conn)
    notify._later(notify.verification_decided(v["user_id"], v["kind"], verification.DOC_NAME.get(v["doc_type"], v["doc_type"]),
                                              action == "approve", reason))
    return {"id": str(v["id"]), "status": v["status"]}


# -------------------------------------------------------------- test bench --
async def _timed(fn) -> dict:
    start = time.monotonic()
    try:
        out = await fn()
        ok, detail = (out if isinstance(out, tuple) else (True, out))
    except Exception as e:
        ok, detail = False, str(e) or e.__class__.__name__
    return {"ok": ok, "detail": detail, "ms": round((time.monotonic() - start) * 1000)}


def _ago(dt) -> str:
    if not dt:
        return "never"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


async def _last_event(prefix: str) -> str:
    r = await db.one(
        """select max(created_at) filter (where ok) as good, max(created_at) filter (where not ok) as bad
             from app.events where kind like %s and created_at > now() - interval '30 days'""", (prefix + "%",))
    return f"last success {_ago(r['good'])}, last failure {_ago(r['bad'])}"


async def _t_database():
    r = await db.one("select count(*) as n from app.profiles")
    return True, f"answered, {r['n']} profiles"


async def _t_schema():
    missing = []
    for table, file in (("app.trades", "003"), ("app.referral_earnings", "004"), ("app.sessions", "005"),
                        ("app.events", "010"), ("app.mpesa_demo_wallet", "013"), ("app.verifications", "015")):
        try:
            await db.one(f"select 1 from {table} limit 1")
        except Exception:
            missing.append(f"sql/{file} ({table})")
    return (False, "missing: " + ", ".join(missing)) if missing else (True, "every migration through 015 is in place")


async def _t_feed():
    snap = prices.snapshot()
    live = sum(1 for v in snap.values() if v["live"])
    return live > 0, f"{live} of {len(prices.MARKETS)} markets ticking"


async def _t_payhero():
    s = get_settings()
    if not s.payhero_ready:
        return False, "not configured: set PAYHERO_API_USERNAME and PAYHERO_API_PASSWORD"
    return True, f"configured, channel {s.payhero_channel_id}; {await _last_event('mpesa')}"


async def _t_kora():
    if not get_settings().kora_ready:
        return False, "not configured: set KORA_SECRET_KEY"
    await kora._call("GET", "/balances")
    return True, "the key was accepted"


async def _t_paystack():
    if not get_settings().paystack_ready:
        return False, "not configured: set PAYSTACK_SECRET_KEY"
    await paystack._call("GET", "/balance")
    return True, "the key was accepted"


async def _t_sms():
    if not get_settings().sms_ready:
        return False, "no gateway configured"
    r = await db.one("""select max(created_at) filter (where status = 'sent') as good,
                               max(created_at) filter (where status = 'failed') as bad
                          from app.notifications where channel = 'sms' and created_at > now() - interval '30 days'""")
    return True, f"{sms.provider()}, sender {sms.sender() or 'default'}; last sent {_ago(r['good'])}, last failure {_ago(r['bad'])}"


async def _t_email():
    s = get_settings()
    if not s.email_ready:
        return False, "RESEND_API_KEY not set"
    r = await db.one("""select max(created_at) filter (where status = 'sent') as good,
                               max(created_at) filter (where status = 'failed') as bad
                          from app.notifications where channel = 'email' and created_at > now() - interval '30 days'""")
    return True, f"from {s.email_from}; last sent {_ago(r['good'])}, last failure {_ago(r['bad'])}"


async def _t_deposits():
    r = await db.one("""select count(*) as n from app.transactions
                         where type = 'deposit' and status = 'pending' and created_at < now() - interval '15 minutes'""")
    return r["n"] == 0, "none stuck" if r["n"] == 0 else f"{r['n']} deposits pending over 15 minutes"


TESTS = {"database": _t_database, "schema": _t_schema, "feed": _t_feed, "payhero": _t_payhero, "kora": _t_kora,
         "paystack": _t_paystack, "sms": _t_sms, "email": _t_email, "deposits": _t_deposits}


@router.get("/tests/{name}")
async def run_test(name: str, _: str = Depends(admin)):
    if name not in TESTS:
        raise HTTPException(status_code=404, detail="No such test.")
    return {"name": name, **await _timed(TESTS[name])}


class TestSend(BaseModel):
    to: str = Field(min_length=3, max_length=120)


@router.post("/tests/send-sms")
async def test_send_sms(body: TestSend, ip: Principal = Depends(admin)):
    """One text to any number, without involving a user's account."""
    need(ip, "admin")
    if not get_settings().sms_ready:
        raise HTTPException(status_code=400, detail="No SMS gateway is configured.")
    start = time.monotonic()
    out = await sms.send(body.to.strip(), "obisflow test bench: this is a test message. No action needed.")
    await _log("test.sms", body.to.strip(), None, f"{out['status']}: {out['detail']}"[:300], ip)
    return {**out, "ms": round((time.monotonic() - start) * 1000), "sender": sms.sender()}


@router.post("/tests/send-email")
async def test_send_email(body: TestSend, ip: Principal = Depends(admin)):
    need(ip, "admin")
    to = body.to.strip()
    if "@" not in to:
        raise HTTPException(status_code=400, detail="Enter an email address.")
    start = time.monotonic()
    ok = await notify._send_email(None, "test", to, "obisflow test bench", notify._page(
        "Test email", ["This is a test from the obisflow admin test bench. No action needed."]),
        "This is a test from the obisflow admin test bench.")
    await _log("test.email", to, None, "sent" if ok else "failed", ip)
    return {"ok": ok, "detail": "sent" if ok else "not sent: see Logs for the reason", "ms": round((time.monotonic() - start) * 1000)}


# ------------------------------------------------------- admin accounts --
# sql/016_admin_users.sql. Passwords are scrypt; a session is a random token
# stored as its SHA-256 for SESSION_HOURS.
SESSION_HOURS = 12


def _pw_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def _pw_ok(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(digest)))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except Exception:
        return False


# a hash to check against when the email is unknown, so a miss takes as long as a wrong password
_DUMMY = _pw_hash(secrets.token_hex(8))


def _gate() -> None:
    if len(get_settings().admin_token) < 32:
        raise HTTPException(status_code=404, detail="Not Found")


def _admin_row(r: dict) -> dict:
    return {"id": str(r["id"]), "email": r["email"], "name": r["name"], "role": r["role"], "active": r["active"],
            "createdAt": _iso(r["created_at"]), "lastLoginAt": _iso(r.get("last_login_at"))}


async def _count_admins() -> int | None:
    try:
        return (await db.one("select count(*) as n from app.admin_users"))["n"]
    except UndefinedTable:
        return None


async def _start_session(admin_id, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    await db.run(
        """insert into app.admin_sessions (token_hash, admin_id, ip, user_agent, expires_at)
           values (%s, %s, %s, %s, now() + make_interval(hours => %s))""",
        (_hash(token), admin_id, _ip(request), request.headers.get("user-agent", "")[:200], SESSION_HOURS))
    await db.run("update app.admin_users set last_login_at = now() where id = %s", (admin_id,))
    if secrets.randbelow(20) == 0:
        await db.run("delete from app.admin_sessions where expires_at < now()")
    return token


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class Setup(BaseModel):
    admin_token: str = Field(min_length=1, max_length=500)
    email: str = Field(min_length=3, max_length=200)
    name: str = Field(default="", max_length=80)
    password: str = Field(min_length=10, max_length=200)


@router.get("/auth/state")
async def auth_state():
    """Whether the first super admin still has to be made (and whether sql/016 is in)."""
    _gate()
    n = await _count_admins()
    return {"ready": n is not None, "setup": n == 0}


@router.post("/auth/setup")
async def auth_setup(body: Setup, request: Request):
    """The first super admin, made with ADMIN_TOKEN while there are no accounts."""
    _gate()
    ip = _ip(request)
    _missed(ip)
    if not hmac.compare_digest(body.admin_token.strip().encode(), get_settings().admin_token.encode()):
        _misses[ip].append(time.monotonic())
        raise HTTPException(status_code=401, detail="That admin token is not right.")
    n = await _count_admins()
    if n is None:
        raise HTTPException(status_code=503, detail="Run sql/016_admin_users.sql on the database first.")
    if n:
        raise HTTPException(status_code=409, detail="The first super admin already exists. Sign in instead.")
    row = await db.one(
        "insert into app.admin_users (email, name, role, password_hash) values (%s, %s, 'superadmin', %s) returning *",
        (body.email.strip().lower(), body.name.strip(), _pw_hash(body.password)))
    who = Principal.make(ip, str(row["id"]), row["email"], row["name"], row["role"])
    await _log("admin.setup", row["email"], None, "first super admin", who)
    return {"token": await _start_session(row["id"], request), "me": _admin_row(row)}


@router.post("/auth/login")
async def auth_login(body: Login, request: Request):
    _gate()
    ip = _ip(request)
    _missed(ip)
    try:
        row = await db.one("select * from app.admin_users where email = %s", (body.email.strip().lower(),))
    except UndefinedTable:
        raise HTTPException(status_code=503, detail="Run sql/016_admin_users.sql on the database first.")
    ok = _pw_ok(body.password, row["password_hash"] if row else _DUMMY)
    if not row or not ok:
        _misses[ip].append(time.monotonic())
        raise HTTPException(status_code=401, detail="That email and password do not match.")
    if not row["active"]:
        raise HTTPException(status_code=403, detail="This admin account is switched off. Ask a super admin.")
    return {"token": await _start_session(row["id"], request), "me": _admin_row(row)}


@router.post("/auth/logout")
async def auth_logout(request: Request, who: Principal = Depends(admin)):
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if who.id:
        await db.run("delete from app.admin_sessions where token_hash = %s", (_hash(given),))
    return {"ok": True}


@router.get("/me")
async def admin_me(who: Principal = Depends(admin)):
    if not who.id:
        return {"id": None, "email": who.email, "name": who.name, "role": who.role, "active": True,
                "createdAt": None, "lastLoginAt": None, "sessions": [], "usdKesRate": get_settings().usd_kes_rate}
    row = await db.one("select * from app.admin_users where id = %s", (who.id,))
    sessions = await db.many(
        "select ip, user_agent, created_at, expires_at from app.admin_sessions where admin_id = %s and expires_at > now() "
        "order by created_at desc", (who.id,))
    return {**_admin_row(row), "usdKesRate": get_settings().usd_kes_rate, "sessions": [{"ip": s["ip"], "device": s["user_agent"], "createdAt": _iso(s["created_at"]),
                                             "expiresAt": _iso(s["expires_at"])} for s in sessions]}


class Profile(BaseModel):
    name: str = Field(max_length=80)


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=10, max_length=200)


def _own(who: Principal) -> str:
    if not who.id:
        raise HTTPException(status_code=400, detail="Signed in with ADMIN_TOKEN: there is no account to change.")
    return who.id


@router.post("/me/profile")
async def admin_profile(body: Profile, who: Principal = Depends(admin)):
    row = await db.one("update app.admin_users set name = %s where id = %s returning *", (body.name.strip(), _own(who)))
    return _admin_row(row)


@router.post("/me/password")
async def admin_password(body: PasswordChange, request: Request, who: Principal = Depends(admin)):
    row = await db.one("select password_hash from app.admin_users where id = %s", (_own(who),))
    if not _pw_ok(body.current, row["password_hash"]):
        raise HTTPException(status_code=400, detail="Your current password is not right.")
    await db.run("update app.admin_users set password_hash = %s where id = %s", (_pw_hash(body.new), who.id))
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    # every other device is signed out; this one stays
    await db.run("delete from app.admin_sessions where admin_id = %s and token_hash <> %s", (who.id, _hash(given)))
    await _log("admin.password", who.email, None, "changed own password", who)
    return {"ok": True}


@router.post("/me/sign-out-others")
async def admin_sign_out_others(request: Request, who: Principal = Depends(admin)):
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    n = await db.run("delete from app.admin_sessions where admin_id = %s and token_hash <> %s", (_own(who), _hash(given)))
    return {"ended": n}


class NewAdmin(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str = Field(default="", max_length=80)
    role: Literal["superadmin", "admin", "finance", "support", "marketer"]
    password: str = Field(min_length=10, max_length=200)


class AdminChange(BaseModel):
    role: Literal["superadmin", "admin", "finance", "support", "marketer"] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)


@router.get("/admins")
async def list_admins(who: Principal = Depends(admin)):
    need(who)
    try:
        rows = await db.many("select * from app.admin_users order by created_at")
    except UndefinedTable:
        raise HTTPException(status_code=503, detail="Run sql/016_admin_users.sql on the database first.")
    return [_admin_row(r) for r in rows]


@router.post("/admins", status_code=201)
async def create_admin(body: NewAdmin, who: Principal = Depends(admin)):
    need(who)
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Enter an email address.")
    try:
        row = await db.one(
            "insert into app.admin_users (email, name, role, password_hash, created_by) values (%s, %s, %s, %s, %s) returning *",
            (email, body.name.strip(), body.role, _pw_hash(body.password), who.id))
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="There is already an admin with that email.")
    await _log("admin.create", email, None, body.role, who)
    sent = await notify.admin_invite(email, body.name.strip(), body.role, body.password, who.name or who.email)
    return {**_admin_row(row), "emailed": sent}


@router.post("/admins/{admin_id}")
async def change_admin(admin_id: str, body: AdminChange, who: Principal = Depends(admin)):
    need(who)
    if admin_id == who.id and (body.active is False or (body.role and body.role != "superadmin")):
        raise HTTPException(status_code=400, detail="You cannot demote or switch off your own account.")
    try:
        row = await db.one("select * from app.admin_users where id = %s", (admin_id,))
    except Exception:
        row = None
    if not row:
        raise HTTPException(status_code=404, detail="No such admin.")
    if row["role"] == "superadmin" and (body.active is False or (body.role and body.role != "superadmin")):
        left = await db.one("select count(*) as n from app.admin_users where role = 'superadmin' and active and id <> %s", (admin_id,))
        if not left["n"]:
            raise HTTPException(status_code=400, detail="There must always be one active super admin.")
    changes = []
    if body.role:
        await db.run("update app.admin_users set role = %s where id = %s", (body.role, admin_id)); changes.append(f"role {body.role}")
    if body.active is not None:
        await db.run("update app.admin_users set active = %s where id = %s", (body.active, admin_id))
        changes.append("switched on" if body.active else "switched off")
    if body.password:
        await db.run("update app.admin_users set password_hash = %s where id = %s", (_pw_hash(body.password), admin_id))
        changes.append("password reset")
    if body.active is False or body.password or body.role:
        await db.run("delete from app.admin_sessions where admin_id = %s", (admin_id,))
    await _log("admin.change", row["email"], None, ", ".join(changes) or "nothing", who)
    sent = None
    if body.password:
        sent = await notify.admin_password_reset(row["email"], row["name"], body.password, who.name or who.email)
    return {**_admin_row(await db.one("select * from app.admin_users where id = %s", (admin_id,))), "emailed": sent}


# ------------------------------------------------------------------ status --
async def _probe(name: str, fn) -> dict:
    start = time.monotonic()
    try:
        state, detail = await fn()
    except Exception as e:
        state, detail = "down", str(e) or e.__class__.__name__
    return {"key": name, "state": state, "detail": detail, "ms": round((time.monotonic() - start) * 1000)}


async def _recent(where: str, args: tuple) -> dict:
    return await db.one(
        f"""select count(*) filter (where ok) as good, count(*) filter (where not ok) as bad,
                   max(created_at) filter (where ok) as last_good, max(created_at) filter (where not ok) as last_bad
              from app.events where created_at > now() - interval '24 hours' and {where}""", args)


def _judge(r: dict, idle: str) -> tuple[str, str]:
    """Up when the latest call worked, warn when the latest one failed, idle when nothing ran."""
    if not r["good"] and not r["bad"]:
        return "idle", idle
    if r["last_bad"] and (not r["last_good"] or r["last_bad"] > r["last_good"]):
        return "warn", f"last call failed {_ago(r['last_bad'])}; {r['good']} ok, {r['bad']} failed in 24 h"
    return "up", f"{r['good']} ok, {r['bad']} failed in 24 h; last ok {_ago(r['last_good'])}"


async def _s_database():
    await db.one("select 1 from app.profiles limit 1")
    return "up", "answering"


async def _s_payments():
    s = get_settings()
    if not (s.payhero_ready or s.kora_ready):
        return "down", "no M-Pesa provider configured"
    stuck = (await db.one("""select count(*) as n from app.transactions where type = 'deposit' and status = 'pending'
                              and created_at < now() - interval '15 minutes'
                              and created_at > now() - interval '24 hours'"""))["n"]
    state, detail = _judge(await _recent("(kind like %s or kind like %s)", ("mpesa%", "webhook%")), "no M-Pesa calls in 24 h")
    if stuck:
        return "warn", f"{stuck} deposits stuck at pending; " + detail
    return state, detail


async def _notes(channel: str) -> dict:
    return await db.one(
        """select count(*) filter (where status = 'sent') as good, count(*) filter (where status = 'failed') as bad,
                  max(created_at) filter (where status = 'sent') as last_good,
                  max(created_at) filter (where status = 'failed') as last_bad
             from app.notifications where channel = %s and created_at > now() - interval '24 hours'""", (channel,))


async def _s_sms():
    if not get_settings().sms_ready:
        return "down", "no SMS gateway configured"
    state, detail = _judge(await _notes("sms"), "no texts sent in 24 h")
    return state, f"{sms.provider()}: {detail}"


async def _s_email():
    if not get_settings().email_ready:
        return "down", "RESEND_API_KEY not set"
    return _judge(await _notes("email"), "no emails sent in 24 h")


async def _s_feed():
    snap = prices.snapshot()
    live = sum(1 for v in snap.values() if v["live"])
    total = len(prices.MARKETS)
    return ("up" if live == total else "warn" if live else "down"), f"{live} of {total} markets ticking"


@router.get("/status")
async def service_status(_: str = Depends(admin)):
    """The key services, each up, warn (working but its latest call failed), idle or down."""
    import asyncio
    checks = {"database": _s_database, "payments": _s_payments, "sms": _s_sms, "email": _s_email, "feed": _s_feed}
    return await asyncio.gather(*(_probe(k, f) for k, f in checks.items()))


# -------------------------------------------------------------- live sessions --
# sql/017_marketers.sql. Real money deposited while a session runs (VIPs left
# out) is the marketer's. Only one session runs at a time.
_LIVE_FIGURES = """
    select l.*, u.name as marketer_name, u.email as marketer_email,
           coalesce(f.usd, 0) as usd, coalesce(f.n, 0) as n, coalesce(f.depositors, 0) as depositors,
           coalesce(g.signups, 0) as signups
      from app.live_sessions l
      join app.admin_users u on u.id = l.marketer_id
      left join lateral (
        select sum(t.net_usd) as usd, count(*) as n, count(distinct t.user_id) as depositors
          from app.transactions t join app.profiles p on p.id = t.user_id
         where t.type = 'deposit' and t.status = 'completed' and not p.is_vip
           and t.completed_at >= l.started_at and t.completed_at < coalesce(l.ended_at, now())) f on true
      left join lateral (
        select count(*) as signups from app.profiles p
         where p.created_at >= l.started_at and p.created_at < coalesce(l.ended_at, now())) g on true"""


def _live_row(r: dict, full: bool) -> dict:
    out = {"id": str(r["id"]), "channel": r["channel"], "note": r["note"], "startedAt": _iso(r["started_at"]),
           "endedAt": _iso(r["ended_at"]), "live": r["ended_at"] is None,
           "deposits": _f(r["usd"]), "depositCount": r["n"]}
    if full:                                            # admins also see who, and how many people
        out.update(marketerId=str(r["marketer_id"]), marketer=r["marketer_name"] or r["marketer_email"],
                   marketerEmail=r["marketer_email"], depositors=r["depositors"], signups=r["signups"])
    return out


@router.get("/live")
async def live_sessions(who: Principal = Depends(admin)):
    mine = who.role == "marketer"
    try:
        rows = await db.many(f"{_LIVE_FIGURES} {'where l.marketer_id = %s' if mine else ''} "
                             "order by l.started_at desc limit 200", (who.id,) if mine else ())
        other = await db.one("select 1 as x from app.live_sessions where ended_at is null and marketer_id is distinct from %s",
                             (who.id,))
        marketers = [] if mine else await db.many(
            "select id, name, email from app.admin_users where role = 'marketer' and active order by name, email")
    except UndefinedTable:
        raise HTTPException(status_code=503, detail="Run sql/017_marketers.sql on the database first.")
    sessions = [_live_row(r, not mine) for r in rows]
    return {"sessions": sessions, "someoneElseLive": bool(other) if mine else None,
            "totals": {"deposits": sum(x["deposits"] for x in sessions), "depositCount": sum(x["depositCount"] for x in sessions),
                       "sessions": len(sessions)},
            "marketers": [{"id": str(m["id"]), "name": m["name"] or m["email"]} for m in marketers],
            "usdKesRate": get_settings().usd_kes_rate}


class GoLive(BaseModel):
    channel: str = Field(default="", max_length=40)
    note: str | None = Field(default=None, max_length=200)
    marketer_id: str | None = None                     # admins start one for a marketer; a marketer starts their own


@router.post("/live/start", status_code=201)
async def go_live(body: GoLive, who: Principal = Depends(admin)):
    if who.role == "marketer":
        marketer = who.id
    else:
        need(who, "admin")
        marketer = body.marketer_id
        try:
            ok = marketer and await db.one("select 1 from app.admin_users where id = %s and role = 'marketer' and active", (marketer,))
        except Exception:
            ok = None
        if not ok:
            raise HTTPException(status_code=400, detail="Choose a marketer.")
    try:
        row = await db.one(
            "insert into app.live_sessions (marketer_id, channel, note, started_by) values (%s, %s, %s, %s) returning id",
            (marketer, body.channel.strip(), (body.note or "").strip() or None, who.id))
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="Another session is live. It has to end first, so money is not counted twice.")
    await _log("live.start", str(row["id"]), None, body.channel.strip() or None, who)
    return {"id": str(row["id"])}


@router.post("/live/{sid}/end")
async def end_live(sid: str, who: Principal = Depends(admin)):
    if who.role != "marketer":
        need(who, "admin")
    try:
        row = await db.one(
            f"""update app.live_sessions set ended_at = now(), ended_by = %s
                 where id = %s and ended_at is null {'and marketer_id = %s' if who.role == 'marketer' else ''}
             returning id""", (who.id, sid, *([who.id] if who.role == "marketer" else [])))
    except Exception:
        row = None
    if not row:
        raise HTTPException(status_code=409, detail="That session is not live.")
    await _log("live.end", sid, None, None, who)
    return {"id": sid}
