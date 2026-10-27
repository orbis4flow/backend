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
