"""Live prices for the chart: the same ticks the server settles contracts on."""
from fastapi import APIRouter, HTTPException, Query

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
