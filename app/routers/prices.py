"""Live prices for the chart: the same ticks the server settles contracts on."""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..services import prices

router = APIRouter(tags=["market data"])


@router.get("/prices")
async def snapshot():
    """The last price of every market whose feed is up. `live` says it is
    recent enough to open a contract at."""
    return prices.snapshot()


@router.get("/prices/history")
async def history(symbol: str, seconds: int = Query(default=900, ge=10, le=3600)):
    if not prices.market(symbol):
        raise HTTPException(status_code=404, detail="That market has no live prices.")
    return prices.history(symbol.strip(), seconds)


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def reading(sym: str, now: float | None = None) -> dict | None:
    """How one market has moved over the last 15 minutes, in numbers anyone can
    check on the chart: the move against its own typical range, where the price
    sits against its 15-minute average, and RSI on one-minute closes."""
    m = prices.market(sym)
    ticks = prices.history(sym, 900)
    if not m or len(ticks) < 120:
        return None
    now = now or ticks[-1][0]
    closes, minute = [], None
    for t, p in ticks:                                   # one close per minute
        k = int(t // 60)
        if k != minute:
            closes.append(float(p)); minute = k
        else:
            closes[-1] = float(p)
    last = float(ticks[-1][1])
    five = [float(p) for t, p in ticks if t >= now - 300]
    move = last - five[0]
    avg = sum(float(p) for _, p in ticks) / len(ticks)
    # a five-minute move measured in the market's own typical five-minute range
    typical = m.vol * (60 ** 0.5)
    score = move / typical if typical else 0.0
    rsi = _rsi(closes)
    up = score >= 0
    pct = move / five[0] * 100 if five[0] else 0.0
    side = "above" if last >= avg else "below"
    why = (f"{'Up' if up else 'Down'} {abs(pct):.3f}% in 5 minutes, {side} its 15-minute average of "
           f"{avg:,.{m.dp}f}" + (f"; RSI {rsi:.0f}" if rsi is not None else "") + ".")
    return {"sym": m.sym, "dir": "Rise" if up else "Fall", "strength": min(99, max(1, round(abs(score) * 40))),
            "tf": "5m", "price": f"{last:,.{m.dp}f}", "changePct": round(pct, 4),
            "rsi": round(rsi) if rsi is not None else None, "why": why}


@router.get("/signals")
async def signals(limit: int = Query(default=6, ge=1, le=20)):
    """The markets moving most clearly right now, strongest first. Always
    answers with the top `limit` while the feed is up: a reading, not a forecast."""
    out = [r for r in (reading(sym) for sym in prices.MARKETS) if r]
    out.sort(key=lambda r: r["strength"], reverse=True)
    return out[:limit]


@router.get("/prices/stream")
async def stream(symbol: str, request: Request):
    """Every tick of one market as server-sent events: data: {"t": seconds, "p": "price"}."""
    m = prices.market(symbol)
    if not m:
        raise HTTPException(status_code=404, detail="That market has no live prices.")

    async def events():
        last = None
        while not await request.is_disconnected():
            try:
                await asyncio.wait_for(prices.next_tick(), timeout=15)
            except asyncio.TimeoutError:
                yield ": still here\n\n"                # keeps proxies from closing a quiet stream
                continue
            now = prices.latest(m.sym)
            if now and now[0] != last:
                last = now[0]
                yield f"data: {json.dumps({'t': round(now[0], 2), 'p': format(now[1].normalize(), 'f')})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
