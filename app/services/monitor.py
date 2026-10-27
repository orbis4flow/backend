"""Uptime for the status page.

Every five minutes each service is checked once and the result kept in
app.status_checks. GET /status turns those into one figure per day: the
share of checks that passed, and how bad the day was. A service that is not
configured is not checked, so it shows "No data" rather than a false green.

A provider counts as up when its API answers at all (any status below 500):
the check asks whether it is reachable, not whether our keys are right.
"""
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .. import db
from ..config import get_settings

log = logging.getLogger("obisflow.monitor")

EVERY_S = 300
NAIROBI = ZoneInfo("Africa/Nairobi")     # days on the status page are Kenyan days
KEEP_DAYS = 120


async def _db(sql: str) -> bool:
    await db.one(sql)
    return True


async def _reach(url: str, **kw) -> bool:
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                 headers={"User-Agent": "orbisflow-status/1.0"}) as c:
        r = await c.get(url, **kw)
    return r.status_code < 500


def _checks() -> dict:
    """service key → a coroutine factory, for the services that are switched on."""
    s = get_settings()
    out = {
        "api": lambda: _db("select 1 from app.profiles limit 1"),
        "trading": lambda: _db("select 1 from app.trades limit 1"),
        "withdraw": lambda: _db("select 1 from app.transactions limit 1"),
        "usdt": lambda: _reach("https://api.trongrid.io/wallet/getnowblock"),
    }
    if s.frontend_url:
        out["app"] = lambda: _reach(s.frontend_url)
        out["academy"] = lambda: _reach(f"{s.frontend_url}/academy")
    if s.mpesa_uses_kora and s.kora_ready:
        out["mpesa"] = lambda: _reach(f"{s.kora_base_url}/charges/DP-STATUS-PROBE",
                                      headers={"Authorization": f"Bearer {s.kora_secret_key}"})
    elif s.payhero_ready:
        out["mpesa"] = lambda: _reach(s.payhero_base_url)
    if s.paystack_ready:
        out["card"] = lambda: _reach("https://api.paystack.co/bank", params={"perPage": 1},
                                     headers={"Authorization": f"Bearer {s.paystack_secret_key}"})
    if s.groq_api_key:
        out["chat"] = lambda: _reach("https://api.groq.com/openai/v1/models",
                                     headers={"Authorization": f"Bearer {s.groq_api_key}"})
    return out


async def check_once() -> None:
    async def one(key, make):
        t0 = time.monotonic()
        try:
            ok = await asyncio.wait_for(make(), timeout=15)
        except Exception as e:
            log.info("status check %s failed: %s", key, e)
            ok = False
        return key, ok, int((time.monotonic() - t0) * 1000)

    results = await asyncio.gather(*(one(k, m) for k, m in _checks().items()))
    for key, ok, ms in results:
        await db.run("insert into app.status_checks (service, ok, latency_ms) values (%s, %s, %s)", (key, ok, ms))


async def watch(stop: asyncio.Event) -> None:
    """Started with the app: a round of checks every five minutes."""
    rounds = 0
    while not stop.is_set():
        try:
            await check_once()
            rounds += 1
            if rounds % 288 == 1:                     # about once a day
                await db.run("delete from app.status_checks where checked_at < now() - make_interval(days => %s)",
                             (KEEP_DAYS,))
        except Exception as e:                        # before sql/007, or the database is down: try again later
            log.debug("status watch: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=EVERY_S)
        except asyncio.TimeoutError:
            pass


def severity(up: float) -> int:
    """0 operational, 1 degraded, 2 partial outage, 3 major outage."""
    if up >= 99.5:
        return 0
    if up >= 97:
        return 1
    if up >= 90:
        return 2
    return 3


async def days(limit: int = 90) -> list[dict]:
    rows = await db.many(
        """select service, (checked_at at time zone 'Africa/Nairobi')::date as day,
                  count(*) as n, count(*) filter (where ok) as good
             from app.status_checks
            where checked_at > now() - make_interval(days => %s)
            group by 1, 2""", (limit + 1,))
    today = datetime.now(NAIROBI).date()
    by: dict[str, list] = {}
    for r in rows:
        up = 100.0 * r["good"] / r["n"]
        by.setdefault(r["service"], []).append(
            {"ago": (today - r["day"]).days, "date": r["day"].isoformat(), "up": round(up, 2),
             "sev": severity(up), "checks": r["n"]})
    return [{"k": k, "days": sorted(v, key=lambda d: d["ago"])} for k, v in by.items()]
