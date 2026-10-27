"""Live prices, held on the server so trades settle on prices the client cannot choose.

  crypto       Binance's public market-data stream (no key): a price a second per coin
  forex, gold, OANDA prices over Finnhub's websocket (the free key in FINNHUB_API_KEY).
  oil, indices A market that sends nothing (closed, or not on the free plan) is simply
               not live, and real money cannot be staked on it
  synthetics   generated here, a tick a second, the same series for everyone

Each market keeps its ticks for a little over an hour (the longest contract),
so a contract settles on the last price at or before its expiry. A feed that
drops and reconnects marks its markets as alive only from then on: a contract
that was open across the gap cannot be priced and is refunded instead.

One process holds the feed, so the API runs as a single instance.
"""
import asyncio
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

import websockets

from ..config import get_settings

log = logging.getLogger("obisflow.prices")

KEEP_S = 3700          # ticks kept per market: the longest contract plus a margin
FRESH_S = 15           # a price older than this is not one to open a contract at


@dataclass(frozen=True)
class Market:
    sym: str
    payout: int                 # % of the stake paid on top when the contract wins
    dp: int                     # decimal places, as the chart shows them
    source: str                 # 'binance', 'finnhub', 'synthetic'
    feed: str = ""              # the symbol at the source
    base: float = 0.0           # synthetics: where the series starts
    vol: float = 0.0            # synthetics: typical move per second
    spikes: bool = False        # synthetics: Boom-style upward jumps


MARKETS: dict[str, Market] = {m.sym: m for m in [
    Market("EUR/USD", 88, 5, "finnhub", "OANDA:EUR_USD"),
    Market("GBP/USD", 87, 5, "finnhub", "OANDA:GBP_USD"),
    Market("USD/JPY", 86, 3, "finnhub", "OANDA:USD_JPY"),
    Market("AUD/USD", 86, 5, "finnhub", "OANDA:AUD_USD"),
    Market("USD/CHF", 86, 5, "finnhub", "OANDA:USD_CHF"),
    Market("USD/CAD", 86, 5, "finnhub", "OANDA:USD_CAD"),
    Market("NZD/USD", 85, 5, "finnhub", "OANDA:NZD_USD"),
    Market("EUR/GBP", 85, 5, "finnhub", "OANDA:EUR_GBP"),
    Market("EUR/JPY", 85, 3, "finnhub", "OANDA:EUR_JPY"),
    Market("GBP/JPY", 84, 3, "finnhub", "OANDA:GBP_JPY"),
    Market("AUD/JPY", 84, 3, "finnhub", "OANDA:AUD_JPY"),
    Market("EUR/CHF", 84, 5, "finnhub", "OANDA:EUR_CHF"),
    Market("USD/ZAR", 80, 4, "finnhub", "OANDA:USD_ZAR"),
    Market("USD/MXN", 80, 4, "finnhub", "OANDA:USD_MXN"),
    Market("BTC/USD", 84, 2, "binance", "BTCUSDT"),
    Market("ETH/USD", 83, 2, "binance", "ETHUSDT"),
    Market("SOL/USD", 82, 2, "binance", "SOLUSDT"),
    Market("XRP/USD", 80, 4, "binance", "XRPUSDT"),
    Market("BNB/USD", 81, 2, "binance", "BNBUSDT"),
    Market("ADA/USD", 79, 4, "binance", "ADAUSDT"),
    Market("DOGE/USD", 78, 5, "binance", "DOGEUSDT"),
    Market("LTC/USD", 80, 3, "binance", "LTCUSDT"),
    Market("DOT/USD", 79, 4, "binance", "DOTUSDT"),
    Market("LINK/USD", 80, 3, "binance", "LINKUSDT"),
    Market("MATIC/USD", 78, 4, "binance", "POLUSDT"),     # Polygon's token is POL now
    Market("XLM/USD", 77, 5, "binance", "XLMUSDT"),
    Market("XAU/USD", 85, 2, "finnhub", "OANDA:XAU_USD"),
    Market("XAG/USD", 82, 3, "finnhub", "OANDA:XAG_USD"),
    Market("WTI", 81, 3, "finnhub", "OANDA:WTICO_USD"),
    Market("US 500", 86, 2, "finnhub", "OANDA:SPX500_USD"),
    Market("US 100", 85, 2, "finnhub", "OANDA:NAS100_USD"),
    Market("UK 100", 84, 2, "finnhub", "OANDA:UK100_GBP"),
    Market("GER 40", 84, 2, "finnhub", "OANDA:DE30_EUR"),
    Market("Volatility 75", 92, 1, "synthetic", base=412880, vol=420),
    Market("Volatility 25", 90, 2, "synthetic", base=2684.3, vol=3.1),
    Market("Boom 500", 89, 2, "synthetic", base=9142.8, vol=8, spikes=True),
]}
# USD/KES and XMR/USD have no free live source: they stay demo-only on the client's chart


@dataclass
class Series:
    ticks: deque = field(default_factory=deque)     # (epoch seconds, Decimal)
    alive_since: float | None = None                # when the feed for it last (re)connected

    def add(self, t: float, p: Decimal) -> None:
        if self.ticks and t < self.ticks[-1][0]:
            t = self.ticks[-1][0]                   # keep time moving forward
        self.ticks.append((t, p))
        cutoff = time.time() - KEEP_S
        while self.ticks and self.ticks[0][0] < cutoff:
            self.ticks.popleft()


_series: dict[str, Series] = {sym: Series() for sym in MARKETS}


def _alive(source: str, on: bool) -> None:
    now = time.time()
    for m in MARKETS.values():
        if m.source == source:
            _series[m.sym].alive_since = now if on else None


def _record(sym: str, t: float, p) -> None:
    try:
        price = Decimal(str(p))
    except Exception:
        return
    if price.is_finite() and price > 0:
        _series[sym].add(t, price)


# ------------------------------------------------------------------ reading --
def market(sym: str) -> Market | None:
    return MARKETS.get((sym or "").strip())


def latest(sym: str) -> tuple[float, Decimal] | None:
    """The last price, if its feed is up and it is recent enough to trade at."""
    s = _series.get(sym)
    if not s or s.alive_since is None or not s.ticks:
        return None
    t, p = s.ticks[-1]
    return (t, p) if time.time() - t <= FRESH_S else None


def at(sym: str, when: float, opened: float) -> Decimal | None:
    """The price at `when`: the last tick at or before it. None unless the feed
    was up the whole time from `opened`, and `when` has passed."""
    s = _series.get(sym)
    if not s or s.alive_since is None or s.alive_since > opened or time.time() < when:
        return None
    price = None
    for t, p in reversed(s.ticks):
        if t <= when:
            price = p
            break
    return price


def snapshot() -> dict:
    out = {}
    now = time.time()
    for sym, s in _series.items():
        if s.alive_since is None or not s.ticks:
            continue
        t, p = s.ticks[-1]
        out[sym] = {"p": format(p.normalize(), "f"), "t": t, "live": now - t <= FRESH_S, "dp": MARKETS[sym].dp,
                    "payout": MARKETS[sym].payout}
    return out


def history(sym: str, seconds: int) -> list[list]:
    """Up to `seconds` of ticks, one a second (the last in each second)."""
    s = _series.get(sym)
    if not s or s.alive_since is None:
        return []
    since = time.time() - seconds
    out: list[list] = []
    for t, p in s.ticks:
        if t < since:
            continue
        sec = int(t)
        if out and out[-1][0] == sec:
            out[-1][1] = format(p.normalize(), "f")
        else:
            out.append([sec, format(p.normalize(), "f")])
    return out


# ------------------------------------------------------------------ feeds --
async def _event(source: str, ok: bool, detail: str) -> None:
    from . import events
    await events.record(f"feed.{source}", ok, provider=source, detail=detail[:300])


async def _binance(stop: asyncio.Event) -> None:
    by_feed = {m.feed.lower(): m.sym for m in MARKETS.values() if m.source == "binance"}
    url = "wss://data-stream.binance.vision/stream?streams=" + "/".join(f"{f}@miniTicker" for f in by_feed)
    backoff = 1
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, open_timeout=15) as ws:
                _alive("binance", True)
                await _event("binance", True, "connected")
                backoff = 1
                async for raw in ws:
                    d = json.loads(raw).get("data") or {}
                    sym = by_feed.get((d.get("s") or "").lower())
                    if sym and d.get("c"):
                        _record(sym, (d.get("E") or time.time() * 1000) / 1000, d["c"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("binance feed dropped: %s", e)
            await _event("binance", False, f"dropped: {e}")
        _alive("binance", False)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def _finnhub(stop: asyncio.Event) -> None:
    key = get_settings().finnhub_api_key
    if not key:
        log.info("no FINNHUB_API_KEY: forex, metals and indices are demo-only")
        return
    by_feed = {m.feed: m.sym for m in MARKETS.values() if m.source == "finnhub"}
    backoff = 1
    while not stop.is_set():
        try:
            async with websockets.connect(f"wss://ws.finnhub.io?token={key}", ping_interval=20,
                                          open_timeout=15) as ws:
                for f in by_feed:
                    await ws.send(json.dumps({"type": "subscribe", "symbol": f}))
                _alive("finnhub", True)
                await _event("finnhub", True, "connected")
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "trade":
                        continue
                    for d in msg.get("data") or []:
                        sym = by_feed.get(d.get("s"))
                        if sym and d.get("p"):
                            _record(sym, (d.get("t") or time.time() * 1000) / 1000, d["p"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("finnhub feed dropped: %s", e)
            await _event("finnhub", False, f"dropped: {e}")
        _alive("finnhub", False)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 4, 120)       # the free plan limits reconnects


async def _synthetic(stop: asyncio.Event) -> None:
    rng = random.SystemRandom()
    level = {m.sym: m.base for m in MARKETS.values() if m.source == "synthetic"}
    _alive("synthetic", True)
    while not stop.is_set():
        now = time.time()
        for sym, p in level.items():
            m = MARKETS[sym]
            step = rng.gauss(0, m.vol)
            if m.spikes and rng.random() < 1 / 500:           # about one spike in 500 ticks
                step = abs(step) + m.vol * 25
            p = max(p * 0.2, p + step)
            level[sym] = p
            _record(sym, now, round(p, m.dp))
        await asyncio.sleep(1)


async def watch(stop: asyncio.Event) -> None:
    await asyncio.gather(_binance(stop), _finnhub(stop), _synthetic(stop))
