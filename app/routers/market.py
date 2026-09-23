"""Market news and the economic calendar, from free sources.

  News:      Finnhub's free plan (finnhub.io, needs a free key in FINNHUB_API_KEY):
             /news?category=general|forex|crypto
  Calendar:  the ForexFactory weekly feed (no key): this week's events with
             impact, forecast and previous figures. It publishes no actuals.

Both are cached here, so every visitor shares one fetch: news for five
minutes, the calendar for an hour (the feed asks to be polled sparingly).
If a source is down, the last good copy is served.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter

from ..config import get_settings

router = APIRouter(tags=["market data"])
log = logging.getLogger("orbisflow.market")

_cache: dict[str, tuple[float, object]] = {}
_locks: dict[str, asyncio.Lock] = {}


async def _cached(key: str, ttl: float, fetch):
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            value = await fetch()
            _cache[key] = (time.monotonic() + ttl, value)
            return value
        except Exception as e:
            log.warning("%s fetch failed: %s", key, e)
            return hit[1] if hit else None          # stale beats nothing


def _ago(ts: int) -> str:
    s = max(0, int(time.time()) - int(ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


# -------------------------------------------------------------------- news --
async def _fetch_news() -> list[dict]:
    key = get_settings().finnhub_api_key
    if not key:
        return []
    out = []
    async with httpx.AsyncClient(timeout=15) as c:
        for cat, tag in (("forex", "Forex"), ("crypto", "Crypto"), ("general", "Markets")):
            r = await c.get("https://finnhub.io/api/v1/news", params={"category": cat, "token": key})
            r.raise_for_status()
            for n in r.json() or []:
                if n.get("headline") and n.get("url"):
                    out.append({"id": n.get("id"), "title": n["headline"].strip(), "source": n.get("source") or "",
                                "url": n["url"], "t": int(n.get("datetime") or 0), "sym": tag,
                                "summary": (n.get("summary") or "")[:280]})
    seen, uniq = set(), []
    for n in sorted(out, key=lambda x: -x["t"]):
        if n["title"] in seen:
            continue
        seen.add(n["title"])
        uniq.append(n)
    return uniq[:40]


@router.get("/news")
async def news():
    items = await _cached("news", 300, _fetch_news) or []
    return [dict(n, ago=_ago(n["t"])) for n in items] or None


# ---------------------------------------------------------------- calendar --
FLAGS = {"USD": "us", "EUR": "eu", "GBP": "gb", "JPY": "jp", "AUD": "au", "CAD": "ca",
         "CHF": "ch", "NZD": "nz", "CNY": "cn"}
IMPACT = {"High": "High", "Medium": "Med", "Low": "Low", "Holiday": "Low"}


async def _fetch_calendar() -> list[dict]:
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "orbisflow/1.0"}) as c:
        r = await c.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
        r.raise_for_status()
        rows = r.json() or []
    out = []
    for e in rows:
        try:
            at = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        ccy = (e.get("country") or "").upper()
        out.append({"at": at.isoformat(), "time": at.strftime("%H:%M"), "day": at.strftime("%a %d %b"),
                    "ccy": ccy, "flag": FLAGS.get(ccy), "event": e.get("title") or "",
                    "impact": IMPACT.get(e.get("impact") or "", "Low"),
                    "forecast": e.get("forecast") or None, "previous": e.get("previous") or None,
                    "actual": e.get("actual") or None})
    return sorted(out, key=lambda x: x["at"])


@router.get("/calendar")
async def calendar(range: Literal["today", "tomorrow", "week"] = "today"):
    items = await _cached("calendar", 3600, _fetch_calendar) or []
    today = datetime.now(timezone.utc).date()
    if range == "today":
        items = [e for e in items if datetime.fromisoformat(e["at"]).date() == today]
    elif range == "tomorrow":
        items = [e for e in items if datetime.fromisoformat(e["at"]).date() == today + timedelta(days=1)]
    else:
        # a week lists its day in front of the time, since the rows cross days
        items = [dict(e, time=f"{e['day']} {e['time']}") for e in items]
    return items or None
