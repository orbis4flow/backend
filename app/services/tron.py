"""Automatic USDT (TRC-20) deposits, on TronGrid's free API.

Everyone sends to the one deposit address, so a transfer on its own does not
say whose it is, and a transaction hash is public the moment it lands: a
"paste your hash" form would let anyone claim a stranger's deposit. Instead
each deposit request gets its own exact amount (50.37 USDT for a $50
request, the cents drawn so no two open requests share one) and an
incoming confirmed transfer of exactly that amount credits that request.

  TronGrid: GET /v1/accounts/{address}/transactions/trc20
            ?only_to=true&only_confirmed=true&contract_address=<USDT>
"""
import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .. import db
from ..config import get_settings
from . import ledger

log = logging.getLogger("orbisflow.tron")
MICRO = Decimal("1000000")          # USDT on TRON has 6 decimals
_last_scan = 0.0
_scan_lock = asyncio.Lock()


def exact_amount(base: Decimal) -> Decimal:
    """The base amount plus a random 1 to 99 cents."""
    return base + Decimal(1 + secrets.randbelow(99)) / 100


async def _transfers(since_ms: int) -> list[dict]:
    s = get_settings()
    headers = {"TRON-PRO-API-KEY": s.trongrid_api_key} if s.trongrid_api_key else {}
    out, fingerprint = [], None
    async with httpx.AsyncClient(timeout=20, headers=headers) as c:
        for _ in range(5):                                  # at most 1,000 transfers per scan
            params = {"only_to": "true", "only_confirmed": "true", "contract_address": s.usdt_contract,
                      "min_timestamp": since_ms, "limit": 200, "order_by": "block_timestamp,asc"}
            if fingerprint:
                params["fingerprint"] = fingerprint
            r = await c.get(f"https://api.trongrid.io/v1/accounts/{s.usdt_deposit_address}/transactions/trc20",
                            params=params)
            r.raise_for_status()
            body = r.json()
            out.extend(body.get("data") or [])
            fingerprint = (body.get("meta") or {}).get("fingerprint")
            if not fingerprint:
                break
    return out


async def scan(force: bool = False) -> int:
    """Match confirmed incoming transfers against open requests. Returns how
    many deposits it credited. Throttled to one scan every 10 seconds."""
    global _last_scan
    if not force and time.monotonic() - _last_scan < 10:
        return 0
    async with _scan_lock:
        _last_scan = time.monotonic()
        s = get_settings()
        open_rows = await db.many(
            """select * from app.transactions
                where provider = 'tron' and type = 'deposit' and status = 'pending'""")
        await _expire(open_rows)
        open_rows = [r for r in open_rows if r["status"] == "pending"]
        if not open_rows:
            return 0
        since = min(r["created_at"] for r in open_rows) - timedelta(minutes=5)
        try:
            transfers = await _transfers(int(since.timestamp() * 1000))
        except Exception as e:
            log.warning("TronGrid scan failed: %s", e)
            return 0

        by_amount = {r["local_amount"]: r for r in open_rows}
        credited = 0
        for t in transfers:
            if t.get("to") != s.usdt_deposit_address or (t.get("token_info") or {}).get("address") != s.usdt_contract:
                continue
            amount = Decimal(t.get("value") or "0") / MICRO
            req = by_amount.get(amount)
            seen_at = datetime.fromtimestamp(int(t.get("block_timestamp") or 0) / 1000, timezone.utc)
            if not req or seen_at < req["created_at"] - timedelta(minutes=2):
                await _unmatched(t, amount)
                continue
            try:
                done = await ledger.complete_deposit(req["reference"], receipt=t["transaction_id"],
                                                     provider_ref=t["transaction_id"])
            except UniqueViolation:                          # this transfer already credited a deposit
                continue
            if done:
                credited += 1
                by_amount.pop(amount, None)
                await db.run("update app.transactions set meta = meta || %s where reference = %s",
                             (Jsonb({"from": t.get("from")}), req["reference"]))
        return credited


async def _expire(rows: list[dict]) -> None:
    """A request past its window, with an hour's grace for a slow sender,
    closes. A transfer that arrives later still shows up as unmatched."""
    s = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=s.usdt_request_minutes + 60)
    for r in rows:
        if r["created_at"] < cutoff:
            await ledger.fail_deposit(r["reference"], "No matching USDT transfer arrived in time")
            r["status"] = "failed"


async def _unmatched(t: dict, amount: Decimal) -> None:
    """A transfer nobody's request matches: kept once, for a person to look at."""
    ref = t.get("transaction_id")
    if await db.one("select 1 from app.webhook_events where provider = 'tron' and reference = %s", (ref,)):
        return
    if await db.one("select 1 from app.transactions where provider = 'tron' and provider_ref = %s", (ref,)):
        return
    await db.run("insert into app.webhook_events (provider, reference, payload, error) values ('tron', %s, %s, %s)",
                 (ref, Jsonb(t), f"unmatched transfer of {amount} USDT"))


async def watch(stop: asyncio.Event) -> None:
    """While requests are open, look every 30 seconds. Started with the app."""
    while not stop.is_set():
        try:
            if await db.one("select 1 from app.transactions where provider = 'tron' and status = 'pending' limit 1"):
                await scan(force=True)
        except Exception as e:                               # before sql/004, or a blip: try again later
            log.debug("tron watch: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
