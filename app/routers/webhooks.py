"""Provider callbacks. Each one is stored as received, then settled only after
the provider confirms it on its own API (see services/settle.py).

  PayHero:  POST <this API>/webhooks/payhero?token={PAYHERO_CALLBACK_TOKEN}
            (sent automatically with every STK push)
  Paystack: POST <this API>/webhooks/paystack
            (set once in the Paystack dashboard: Settings → API Keys & Webhooks)
"""
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb

from .. import db
from ..config import get_settings
from ..services import paystack, settle
from ..services.payhero import parse_callback

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = logging.getLogger("orbisflow.webhooks")


async def _record(provider: str, reference: str | None, payload: dict) -> int:
    row = await db.one("insert into app.webhook_events (provider, reference, payload) values (%s, %s, %s) returning id",
                       (provider, reference, Jsonb(payload)))
    return row["id"]


async def _done(event_id: int, error: str | None = None) -> None:
    await db.run("update app.webhook_events set processed = %s, error = %s where id = %s",
                 (error is None, error, event_id))


@router.post("/payhero")
async def payhero_callback(request: Request, token: str = ""):
    s = get_settings()
    if not s.payhero_callback_token or not hmac.compare_digest(token, s.payhero_callback_token):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="bad payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="bad payload")

    info = parse_callback(payload)
    event = await _record("payhero", info["reference"], payload)
    tx = await settle.by_reference(info["reference"] or "")
    if not tx:
        await _done(event, "unknown reference")
        return {"ok": True}
    try:
        state = await settle.payhero_check(tx, info)
        await _done(event, None if state in ("completed", "failed") else f"left {state}: PayHero has not confirmed yet")
    except Exception as e:                       # stored, so it can be looked at and replayed
        log.exception("payhero callback %s", info["reference"])
        await _done(event, str(e)[:300])
    return {"ok": True}


@router.post("/paystack")
async def paystack_callback(request: Request):
    raw = await request.body()
    if not paystack.signature_ok(raw, request.headers.get("x-paystack-signature")):
        raise HTTPException(status_code=401, detail="bad signature")
    payload = json.loads(raw or b"{}")
    data = payload.get("data") or {}
    event = await _record("paystack", data.get("reference"), payload)

    if payload.get("event") not in ("charge.success", "charge.failed"):
        await _done(event)
        return {"ok": True}
    tx = await settle.by_reference(data.get("reference") or "")
    if not tx:
        await _done(event, "unknown reference")
        return {"ok": True}
    try:
        state = await settle.paystack_check(tx)          # re-read from Paystack, not from the webhook body
        await _done(event, None if state in ("completed", "failed") else f"left {state}")
    except Exception as e:
        log.exception("paystack webhook %s", data.get("reference"))
        await _done(event, str(e)[:300])
    return {"ok": True}
