"""Deposits nobody finished are removed.

A deposit still pending (or processing) DEPOSIT_EXPIRE_S after it was
started (CHECKOUT_EXPIRE_S for one paid on a provider's page: cards and
Kora's checkout, which take longer to fill in) is asked about once more, so
one that was paid is credited rather than lost, and is deleted if it is
still open after that. USDT deposits are
left alone: they wait for a transfer on the TRON chain for
USDT_REQUEST_MINUTES (services/tron.py).

A provider callback for a deposit that was deleted is kept in
app.webhook_events and shows on the admin site's Logs as "unknown reference".
"""
import asyncio
import logging

from .. import db
from ..config import get_settings
from . import events, settle

log = logging.getLogger("obisflow.deposits")

EVERY = 30          # seconds between sweeps


async def _ask(tx: dict) -> str:
    if tx["provider"] == "kora":
        return await settle.kora_check(tx)
    if tx["provider"] == "payhero":
        return await settle.payhero_check(tx)
    if tx["provider"] == "paystack":
        return await settle.paystack_check(tx)
    return tx["status"]


async def expire() -> int:
    """Delete the unfinished deposits that are old enough. Returns how many went."""
    s = get_settings()
    rows = await db.many(
        """select *, case when provider = 'paystack' or meta ? 'checkout' then %s else %s end as expire_s
             from app.transactions
            where type = 'deposit' and status in ('pending', 'processing') and provider <> 'tron'
              and created_at < now() - make_interval(secs => case when provider = 'paystack' or meta ? 'checkout'
                                                                  then %s else %s end)
            order by created_at limit 100""",
        (s.checkout_expire_s, s.deposit_expire_s, s.checkout_expire_s, s.deposit_expire_s))
    gone = 0
    for tx in rows:
        try:
            if await _ask(tx) not in ("pending", "processing"):
                continue                                 # it settled just now, one way or the other
        except Exception:
            log.exception("last check of %s failed", tx["reference"])
            continue                                     # leave it for the next sweep
        row = await db.one(
            """delete from app.transactions where id = %s and status in ('pending', 'processing')
               returning reference""", (tx["id"],))
        if row:
            gone += 1
            await events.record("deposit.expired", True, provider=tx["provider"], ref=tx["reference"],
                                user_id=str(tx["user_id"]),
                                detail=f"${tx['amount_usd']} by {tx['method']}, unfinished after {tx['expire_s'] // 60} min: removed")
    return gone


async def watch(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await expire()
        except Exception as e:                           # no database yet, or it blinked
            log.debug("deposit sweep: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=EVERY)
        except asyncio.TimeoutError:
            pass
