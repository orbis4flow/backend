"""The M-Pesa clone handset (the VIP demo rail, services/mpesa_demo.py).

The same routes and answers as Nexas's /mpesa/*, so the clone app's Novi rail
works against this API with only its base URL changed. The phone has no
session and no account: it sends the PIN once (POST /mpesa/link), is given a
device token, and sends that as a bearer token from then on. Refusals answer
{"ok": false, "error": {"code", "message"}}, which is what the app reads; 401
means the link is gone and the app asks for the PIN again.
"""
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..services import events, mpesa_demo as demo

router = APIRouter(prefix="/mpesa", tags=["mpesa demo"], include_in_schema=False)

_tries: dict[str, deque] = defaultdict(deque)
LINK_LIMIT, LINK_WINDOW = 10, 15 * 60


def _refuse(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": {"code": code, "message": message}})


async def handset(request: Request) -> dict:
    """The wallet behind the device token on the request."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    value = token if scheme.lower() == "bearer" else request.headers.get("x-device-token", "")
    if not value.strip():
        raise demo.RailError(401, "unauthorized", "This phone is not linked yet.")
    wallet = await demo.by_token(value.strip())
    if not wallet:
        raise demo.RailError(401, "unauthorized", "This phone is no longer linked. Enter the PIN again.")
    return wallet


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "")


class Link(BaseModel):
    pin: str = Field(pattern=r"^\d{4}$")


class Amount(BaseModel):
    amountMinor: int = Field(gt=0, le=100_000_000)
    agent: str | None = Field(default=None, max_length=60)
    # the Venti rail's body, so either handset dialect works
    chargeMinor: int | None = Field(default=None, ge=0)
    agentName: str | None = Field(default=None, max_length=60)


class Receive(BaseModel):
    amountMinor: int = Field(gt=0, le=100_000_000)
    from_: str | None = Field(default=None, max_length=60, alias="from")


class Reset(BaseModel):
    balanceMinor: int | None = Field(default=None, ge=0)


@router.post("/link")
async def link(body: Link, request: Request):
    """A wrong PIN and an unassigned one get the same answer, and tries are
    limited, so the endpoint cannot be used to find which PINs exist."""
    ip = _ip(request)
    tries = _tries[ip]
    while tries and tries[0] < time.monotonic() - LINK_WINDOW:
        tries.popleft()
    if len(tries) >= LINK_LIMIT:
        return _refuse(429, "too_many", "Too many tries. Wait 15 minutes and try again.")
    tries.append(time.monotonic())
    wallet = await demo.by_pin(body.pin)
    if not wallet:
        await events.record("mpesa_demo.link", False, provider="mpesa_demo", detail="unknown PIN")
        return _refuse(400, "bad_request", "That PIN is not recognised.")
    await events.record("mpesa_demo.link", True, provider="mpesa_demo", user_id=wallet["userId"],
                        detail="handset linked")
    return {"ok": True, "deviceToken": wallet["deviceToken"], "account": await demo.handset_view(wallet)}


@router.get("/account")
async def account(wallet: dict = Depends(handset)):
    return {"ok": True, "account": await demo.handset_view(wallet)}


@router.post("/agent-withdraw")
async def agent_withdraw(body: Amount, wallet: dict = Depends(handset)):
    """Cash out at an agent: the handset spending its own balance. Nothing on
    the trading side moves. Novi's app sends the charge inside amountMinor;
    Venti's sends it apart, and it is added here."""
    amount = body.amountMinor + (body.chargeMinor or 0)
    moved = await demo.move_now(wallet["userId"], kind="AGENT_WITHDRAWAL", amount_minor=amount, direction="OUT",
                                title="Withdraw at agent", subtitle=body.agent or body.agentName or "Agent withdrawal")
    fresh = await demo.wallet_for(wallet["userId"])
    return {"ok": True, **_moved(moved), "account": await demo.handset_view(fresh)}


@router.post("/receive")
async def receive(body: Receive, wallet: dict = Depends(handset)):
    """Money arriving on the phone, so a demo can top the wallet up. Fuliza is repaid first."""
    moved = await demo.move_now(wallet["userId"], kind="RECEIVE", amount_minor=body.amountMinor, direction="IN",
                                title="Received money", subtitle=body.from_ or "Deposit")
    fresh = await demo.wallet_for(wallet["userId"])
    return {"ok": True, **_moved(moved), "account": await demo.handset_view(fresh)}


@router.post("/reset")
async def reset(body: Reset, wallet: dict = Depends(handset)):
    """Between rehearsals: an empty statement, nothing owed, the handset still linked."""
    fresh = await demo.reset(wallet["userId"], body.balanceMinor)
    return {"ok": True, "account": await demo.handset_view(fresh)}


def _moved(m: dict) -> dict:
    return {"balanceMinor": m["balanceMinor"], "fulizaUsedMinor": m["fulizaUsedMinor"],
            "fulizaLimitMinor": m["fulizaLimitMinor"], "tx": m["tx"]}
