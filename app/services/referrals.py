"""Referral earnings: counted weekly, paid every Thursday.

For each referrer and each week (Monday to Sunday, Nairobi time):
  volume   = real-money stakes their referrals placed that week
  spread   = volume x REFERRAL_SPREAD_PCT (orbisflow's earnings on it)
  tier     = 20%, 28% or 35%, by referrals who traded real money in the
             30 days to the end of that week: 1-9, 10-49, 50 or more
  amount   = spread x tier

The running week is recounted as trades come in and shows as pending. A
week that has ended is paid into the referrer's real balance from the
Thursday after it, as its own transaction, once. Demo trades earn nothing,
so nothing accrues until real-money trading opens.
"""
import asyncio
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .. import db
from ..config import get_settings
from ..util import NAIROBI, reference, usd

log = logging.getLogger("orbisflow.referrals")
TIERS = ((50, Decimal("35")), (10, Decimal("28")), (1, Decimal("20")))


def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _bounds(week_start: date) -> tuple[datetime, datetime]:
    start = datetime.combine(week_start, time.min, NAIROBI)
    return start, start + timedelta(days=7)


def payday(week_start: date) -> datetime:
    """The Thursday after the week ends, at the start of the day in Nairobi."""
    return datetime.combine(week_start + timedelta(days=10), time.min, NAIROBI)


def tier_for(active_30d: int) -> Decimal:
    for floor, pct in TIERS:
        if active_30d >= floor:
            return pct
    return Decimal("0")


def period(week_start: date) -> str:
    end = week_start + timedelta(days=6)
    if week_start.month == end.month:
        return f"{week_start.day}-{end.day} {end.strftime('%b %Y')}"
    return f"{week_start.day} {week_start.strftime('%b')} - {end.day} {end.strftime('%b %Y')}"


async def count_week(week_start: date) -> int:
    """(Re)count one week for every referrer. Paid weeks are never touched."""
    start, end = _bounds(week_start)
    spread_pct = Decimal(str(get_settings().referral_spread_pct))
    rows = await db.many(
        """select p.referred_by as referrer,
                  count(distinct t.user_id) as active,
                  coalesce(sum(t.stake), 0) as volume
             from app.trades t
             join app.profiles p on p.id = t.user_id
            where p.referred_by is not null and t.account_kind = 'real' and t.status <> 'open'
              and t.settled_at >= %s and t.settled_at < %s
            group by p.referred_by""", (start, end))
    for r in rows:
        active_30 = await db.one(
            """select count(distinct t.user_id) as n
                 from app.trades t join app.profiles p on p.id = t.user_id
                where p.referred_by = %s and t.account_kind = 'real' and t.status <> 'open'
                  and t.settled_at >= %s and t.settled_at < %s""",
            (r["referrer"], end - timedelta(days=30), end))
        tier = tier_for(active_30["n"])
        spread = usd(r["volume"] * spread_pct / 100)
        amount = usd(spread * tier / 100)
        await db.run(
            """insert into app.referral_earnings (referrer_id, week_start, active, volume, spread, tier_pct, amount)
               values (%s, %s, %s, %s, %s, %s, %s)
               on conflict (referrer_id, week_start) do update
                 set active = excluded.active, volume = excluded.volume, spread = excluded.spread,
                     tier_pct = excluded.tier_pct, amount = excluded.amount, updated_at = now()
               where app.referral_earnings.status = 'pending'""",
            (r["referrer"], week_start, r["active"], r["volume"], spread, tier, amount))
    return len(rows)


async def pay_due() -> int:
    """Pay every pending week whose Thursday has come. One transaction per week, once."""
    from . import notify
    now = datetime.now(NAIROBI)
    due = await db.many(
        "select * from app.referral_earnings where status = 'pending' and week_start <= %s order by week_start",
        (now.date() - timedelta(days=10),))
    paid = 0
    for e in due:
        if now < payday(e["week_start"]):
            continue
        async with db.tx() as conn:
            cur = await conn.execute(
                "update app.referral_earnings set status = 'paid', paid_at = now() "
                "where id = %s and status = 'pending' returning *", (e["id"],))
            row = await cur.fetchone()
            if not row or row["amount"] <= 0:
                continue                                    # nothing to pay: closed as paid at zero
            cur = await conn.execute(
                "select id from app.accounts where user_id = %s and kind = 'real'", (e["referrer_id"],))
            acct = await cur.fetchone()
            cur = await conn.execute(
                """insert into app.transactions (user_id, account_id, type, method, provider, status, amount_usd,
                                                 fee_usd, net_usd, reference, destination, completed_at, meta)
                   values (%s, %s, 'referral', 'referral', 'orbisflow', 'completed', %s, 0, %s, %s, %s, now(),
                           jsonb_build_object('week_start', %s::text))
                returning id""",
                (e["referrer_id"], acct["id"], row["amount"], row["amount"], reference("RF"),
                 "Referral earnings " + period(e["week_start"]), e["week_start"]))
            tx_id = (await cur.fetchone())["id"]
            await conn.execute("update app.accounts set balance = balance + %s where id = %s",
                               (row["amount"], acct["id"]))
            await conn.execute("update app.referral_earnings set transaction_id = %s where id = %s",
                               (tx_id, e["id"]))
        paid += 1
        notify._later(notify.referral_paid(e["referrer_id"], row["amount"], period(e["week_start"])))
    return paid


async def work(stop: asyncio.Event) -> None:
    """Hourly: recount this week and last week, then pay what is due."""
    while not stop.is_set():
        try:
            today = datetime.now(NAIROBI).date()
            await count_week(week_of(today))
            await count_week(week_of(today) - timedelta(days=7))
            n = await pay_due()
            if n:
                log.info("paid %s referral weeks", n)
        except Exception as e:                               # before sql/003 and 004, or a blip
            log.debug("referral job: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
