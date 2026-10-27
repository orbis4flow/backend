"""obisflow's own price feed. Every market is generated here, twice a second,
the same series for everyone; contracts are placed and settled on it, so the
client can never choose a price.

These are simulated prices, not quotes from the real markets the symbols are
named after, and the site and the Terms say so. The generator draws from the
operating system's random source and never looks at open positions, so it
cannot lean against anyone's contract.

Each tick is a step of a random walk with three slow-moving parts, which is
what makes the candles look like a market rather than noise:
  regime   volatility drifts between calm and busy spells
  drift    a small trend that wanders, so moves run for a while, then fade
  anchor   a very weak pull back to the market's base price, so a series
           cannot wander off over days and weeks
Boom 500 also jumps up now and then.

Ticks are kept for a little over an hour (the longest contract), so a contract
settles on the last price at or before its expiry. The last price of each
market is saved every few seconds (sql/011_market_prices.sql), so a restart
carries on from there; a contract open across a restart cannot be priced and
is refunded.

One process holds the feed, so the API runs as a single instance.
"""
import asyncio
import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from .. import db

log = logging.getLogger("obisflow.prices")

TICK_S = 0.5           # a new price for every market this often
KEEP_S = 3700          # ticks kept per market: the longest contract plus a margin
FRESH_S = 5            # a price older than this means the generator has stopped
BACKFILL_S = 900       # history drawn on start, so the chart is never empty
SAVE_EVERY_S = 5


@dataclass(frozen=True)
class Market:
    sym: str
    payout: int                 # % of the stake paid on top when the contract wins
    dp: int                     # decimal places, as the chart shows them
    base: float                 # where the series starts, and the level it is anchored to
    vol: float                  # typical move per five-second candle
    spikes: bool = False        # Boom-style upward jumps


MARKETS: dict[str, Market] = {m.sym: m for m in [
    Market("EUR/USD", 88, 5, 1.0842, 0.0016),
    Market("GBP/USD", 87, 5, 1.2714, 0.0019),
    Market("USD/JPY", 86, 3, 151.38, 0.18),
    Market("AUD/USD", 86, 5, 0.6628, 0.0014),
    Market("USD/CHF", 86, 5, 0.9042, 0.0012),
    Market("USD/CAD", 86, 5, 1.3684, 0.0015),
    Market("NZD/USD", 85, 5, 0.6124, 0.0013),
    Market("EUR/GBP", 85, 5, 0.8528, 0.0009),
    Market("EUR/JPY", 85, 3, 164.12, 0.21),
    Market("GBP/JPY", 84, 3, 192.46, 0.28),
    Market("AUD/JPY", 84, 3, 100.34, 0.16),
    Market("EUR/CHF", 84, 5, 0.9804, 0.0008),
    Market("USD/ZAR", 80, 4, 18.412, 0.06),
    Market("USD/KES", 79, 3, 129.24, 0.24),
    Market("USD/MXN", 80, 4, 16.842, 0.05),
    Market("BTC/USD", 84, 2, 67240, 120),
    Market("ETH/USD", 83, 2, 3412.6, 9),
    Market("SOL/USD", 82, 2, 168.42, 1.4),
    Market("XRP/USD", 80, 4, 0.5284, 0.012),
    Market("BNB/USD", 81, 2, 592.4, 7),
    Market("ADA/USD", 79, 4, 0.4612, 0.011),
    Market("DOGE/USD", 78, 5, 0.1582, 0.006),
    Market("LTC/USD", 80, 3, 84.26, 1.2),
    Market("DOT/USD", 79, 4, 6.842, 0.14),
    Market("LINK/USD", 80, 3, 17.284, 0.32),
    Market("MATIC/USD", 78, 4, 0.7142, 0.015),
    Market("XLM/USD", 77, 5, 0.1124, 0.004),
    Market("XMR/USD", 78, 2, 162.84, 2.1),
    Market("XAU/USD", 85, 2, 2318.4, 3.4),
    Market("XAG/USD", 82, 3, 27.412, 0.06),
    Market("WTI", 81, 3, 78.93, 0.12),
    Market("US 500", 86, 2, 5308.2, 5.2),
    Market("US 100", 85, 2, 18642, 22),
    Market("UK 100", 84, 2, 8214.6, 7),
    Market("GER 40", 84, 2, 18492, 19),
    Market("Volatility 75", 92, 1, 412880, 420),
    Market("Volatility 25", 90, 2, 2684.3, 3.1),
    Market("Boom 500", 89, 2, 9142.8, 8, spikes=True),
]}


@dataclass
class Series:
    ticks: deque = field(default_factory=deque)     # (epoch seconds, Decimal)
    alive_since: float | None = None                # when the generator (re)started

    def add(self, t: float, p: Decimal) -> None:
        if self.ticks and t < self.ticks[-1][0]:
            t = self.ticks[-1][0]                   # keep time moving forward
        self.ticks.append((t, p))
        cutoff = time.time() - KEEP_S
        while self.ticks and self.ticks[0][0] < cutoff:
            self.ticks.popleft()


_series: dict[str, Series] = {sym: Series() for sym in MARKETS}
_tick = asyncio.Event()                             # set on every tick, for the live stream


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
    """The last price, if the generator is running and it is recent enough to trade at."""
    s = _series.get(sym)
    if not s or s.alive_since is None or not s.ticks:
        return None
    t, p = s.ticks[-1]
    return (t, p) if time.time() - t <= FRESH_S else None


def at(sym: str, when: float, opened: float) -> Decimal | None:
    """The price at `when`: the last tick at or before it. None unless the
    generator ran the whole time from `opened`, and `when` has passed."""
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
    """Up to `seconds` of ticks, oldest first, as [epoch seconds, price]."""
    s = _series.get(sym)
    if not s or s.alive_since is None:
        return []
    since = time.time() - seconds
    return [[round(t, 2), format(p.normalize(), "f")] for t, p in s.ticks if t >= since]


async def next_tick() -> None:
    """Wait for the next tick (for the live stream)."""
    await _tick.wait()


# --------------------------------------------------------------- generator --
@dataclass
class _Walk:
    p: float
    regime: float = 0.0          # log of the volatility multiplier
    drift: float = 0.0           # per second, in units of the market's per-second sigma


SIGMA_PER_VOL = 0.28            # per-second sigma, as a share of the market's per-candle vol
ANCHOR_HALF_LIFE_S = 6 * 3600   # how slowly a series is pulled back to its base


def _step(m: Market, w: _Walk, rng: random.Random, dt: float) -> float:
    sigma = m.vol * SIGMA_PER_VOL
    # calm and busy spells: the multiplier wanders slowly between about 0.7 and 1.5,
    # so candles keep a steady size from one minute to the next
    w.regime += -0.003 * w.regime * dt + rng.gauss(0, 0.015) * math.sqrt(dt)
    w.regime = max(-0.35, min(0.4, w.regime))
    # a gentle wandering trend: moves lean one way for a minute or two, then fade
    w.drift += -0.01 * w.drift * dt + rng.gauss(0, 0.005) * math.sqrt(dt)
    w.drift = max(-0.15, min(0.15, w.drift))
    anchor = (m.base - w.p) * (math.log(2) / ANCHOR_HALF_LIFE_S) * dt
    move = sigma * math.exp(w.regime) * (w.drift * dt + rng.gauss(0, 1) * math.sqrt(dt))
    if m.spikes and rng.random() < dt / 500:        # about one spike every eight minutes
        move += sigma * rng.uniform(15, 40)
    return max(m.base * 0.2, w.p + move + anchor)


async def _load_levels() -> dict[str, float]:
    try:
        # a slow database must not hold the prices up: past five seconds, start from the bases
        rows = await asyncio.wait_for(db.many("select sym, price from app.market_prices"), timeout=5)
        return {r["sym"]: float(r["price"]) for r in rows if r["sym"] in MARKETS}
    except Exception as e:
        log.info("starting every market at its base price (%s)", e)
        return {}


async def _save_levels(walks: dict[str, _Walk]) -> None:
    try:
        async with db.tx() as conn:
            for sym, w in walks.items():
                await conn.execute(
                    """insert into app.market_prices (sym, price, updated_at) values (%s, %s, now())
                       on conflict (sym) do update set price = excluded.price, updated_at = now()""",
                    (sym, round(w.p, MARKETS[sym].dp)))
    except Exception as e:
        log.debug("market prices not saved: %s", e)


def _backfill(m: Market, end: float, now: float, rng: random.Random) -> None:
    """Draw BACKFILL_S of history that ends exactly where the series carries on."""
    walk = _Walk(end)
    path = [end]
    for _ in range(int(BACKFILL_S / TICK_S)):
        walk.p = _step(m, walk, rng, TICK_S)
        path.append(walk.p)
    path.reverse()                                   # the walk run backwards ends at `end`
    start = now - TICK_S * (len(path) - 1)
    for i, p in enumerate(path):
        _record(m.sym, start + i * TICK_S, round(p, m.dp))


async def watch(stop: asyncio.Event) -> None:
    global _tick
    rng = random.SystemRandom()
    levels = await _load_levels()
    now = time.time()
    walks = {sym: _Walk(levels.get(sym, m.base)) for sym, m in MARKETS.items()}
    for sym, m in MARKETS.items():
        _backfill(m, walks[sym].p, now, rng)
        _series[sym].alive_since = now               # contracts from before a restart are refunded
    saved = time.monotonic()
    while not stop.is_set():
        now = time.time()
        for sym, m in MARKETS.items():
            w = walks[sym]
            w.p = _step(m, w, rng, TICK_S)
            _record(sym, now, round(w.p, m.dp))
        _tick.set()
        _tick = asyncio.Event()
        if time.monotonic() - saved >= SAVE_EVERY_S:
            saved = time.monotonic()
            asyncio.get_running_loop().create_task(_save_levels(walks))
        await asyncio.sleep(TICK_S)
