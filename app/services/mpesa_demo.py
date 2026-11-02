"""The VIP M-Pesa demo rail, as on Nexas (sql/013_vip_mpesa.sql).

A VIP's M-Pesa deposits come off a demo wallet that the companion M-Pesa clone
app shows, and its withdrawals land back on it, at once, with no provider and
no real money. The handset links once with a four digit PIN the admin site
assigns, is given a device token, and presents that token from then on.

Every movement goes through move(), inside the caller's transaction, under a
row lock on the wallet, and a Safaricom-shaped text follows it once the
transaction has committed (notify_moved). Fuliza works as the real product
does: money out the balance cannot cover draws on the limit and the balance
floors at zero; money in repays what is owed before it reaches the balance.

Amounts are KES minor units (cents) throughout, as the handset counts them.
"""
import logging
import re
import secrets
import uuid
from datetime import datetime, timezone

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from .. import db
from ..config import get_settings
from ..util import NAIROBI

log = logging.getLogger("obisflow.mpesa_demo")

MAX_HISTORY = 60
KINDS = ("DEPOSIT", "WITHDRAWAL", "AGENT_WITHDRAWAL", "RECEIVE", "FULIZA_REPAY", "REVERSAL")


class RailError(Exception):
    """A refusal the handset or the cashier can show as is."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def no_wallet() -> RailError:
    return RailError(409, "no_wallet", "This VIP account has no M-Pesa demo wallet yet. An admin sets its PIN and "
                                       "opening balance on the admin site.")


def insufficient() -> RailError:
    return RailError(400, "insufficient_funds", "There is not enough money in that M-Pesa account.")


REFERENCE_LENGTH = 10


def reference_prefix() -> str:
    """REFERENCE_PREFIX, up to 4 letters or digits, uppercased; "T" when unset
    (as on Venti). Changing it changes new codes only."""
    p = re.sub(r"[^A-Z0-9]", "", (get_settings().reference_prefix or "").upper())[:4]
    return p or "T"


def demo_reference() -> str:
    """A ten character receipt code: the prefix, then random letters and digits."""
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out = reference_prefix()
    return out + "".join(secrets.choice(chars) for _ in range(REFERENCE_LENGTH - len(out)))


def kes_minor(kes: int | float) -> int:
    return int(round(float(kes) * 100))


# ------------------------------------------------------------------ shapes --
def wallet_shape(r: dict | None) -> dict | None:
    if not r:
        return None
    return {"userId": str(r["user_id"]), "deviceToken": str(r["device_token"]), "pin": r["pin"],
            "balanceMinor": int(r["balance_minor"]), "fulizaLimitMinor": int(r["fuliza_limit_minor"]),
            "fulizaUsedMinor": int(r["fuliza_used_minor"]), "holderName": r["holder_name"], "phone": r["phone"],
            "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None}


def tx_shape(r: dict) -> dict:
    return {"id": str(r["id"]), "kind": r["kind"], "title": r["title"], "subtitle": r["subtitle"],
            "amountMinor": int(r["amount_minor"]), "balanceAfterMinor": int(r["balance_after_minor"]),
            "fulizaAfterMinor": int(r["fuliza_after_minor"] or 0), "reference": r["reference"],
            "at": r["created_at"].isoformat()}


# ------------------------------------------------------------------- reads --
async def wallet_for(user_id: str, conn: AsyncConnection | None = None) -> dict | None:
    return wallet_shape(await db.one("select * from app.mpesa_demo_wallet where user_id = %s", (user_id,), conn=conn))


async def by_pin(pin: str) -> dict | None:
    return wallet_shape(await db.one("select * from app.mpesa_demo_wallet where pin = %s", (pin,)))


async def by_token(token: str) -> dict | None:
    try:
        uuid.UUID(token)
    except (ValueError, TypeError):
        return None
    return wallet_shape(await db.one("select * from app.mpesa_demo_wallet where device_token = %s", (token,)))


async def statement(user_id: str, limit: int = MAX_HISTORY) -> list[dict]:
    rows = await db.many("select * from app.mpesa_demo_tx where user_id = %s order by created_at desc limit %s",
                         (user_id, min(MAX_HISTORY, limit)))
    return [tx_shape(r) for r in rows]


async def handset_view(wallet: dict) -> dict:
    """Everything the handset renders in one read, since it polls (Nexas's shape,
    which the clone app's Novi rail already parses)."""
    return {"balanceMinor": wallet["balanceMinor"],
            "fuliza": {"limitMinor": wallet["fulizaLimitMinor"], "usedMinor": wallet["fulizaUsedMinor"],
                       "availableMinor": max(0, wallet["fulizaLimitMinor"] - wallet["fulizaUsedMinor"])},
            "holderName": wallet["holderName"] or "M-PESA user",
            "phone": wallet["phone"] or None,
            "statement": await statement(wallet["userId"])}


# ---------------------------------------------------------------- movement --
async def move(conn: AsyncConnection, user_id: str, kind: str, amount_minor: int, direction: str,
               title: str, subtitle: str = "", reference: str | None = None) -> dict:
    """One movement on one wallet, inside the caller's transaction. Raises
    RailError when there is no wallet or not enough money (Fuliza included)."""
    if kind not in KINDS or direction not in ("IN", "OUT"):
        raise ValueError("bad movement")
    if amount_minor <= 0:
        raise RailError(400, "bad_amount", "Enter an amount above zero.")
    cur = await conn.execute(
        "select balance_minor, fuliza_limit_minor, fuliza_used_minor from app.mpesa_demo_wallet "
        "where user_id = %s for update", (user_id,))
    w = await cur.fetchone()
    if not w:
        raise no_wallet()
    balance, limit, used = int(w["balance_minor"]), int(w["fuliza_limit_minor"]), int(w["fuliza_used_minor"])
    if direction == "OUT":
        signed = -amount_minor
        if amount_minor > balance:
            short = amount_minor - balance
            if used + short > limit:
                raise insufficient()
            used, balance = used + short, 0
        else:
            balance -= amount_minor
    else:
        signed = amount_minor
        repay = min(used, amount_minor)                  # money in clears the overdraft first
        used, balance = used - repay, balance + amount_minor - repay

    await conn.execute("update app.mpesa_demo_wallet set balance_minor = %s, fuliza_used_minor = %s, "
                       "updated_at = now() where user_id = %s", (balance, used, user_id))
    cur = await conn.execute(
        """insert into app.mpesa_demo_tx (user_id, kind, title, subtitle, amount_minor, balance_after_minor,
                                          fuliza_after_minor, reference)
           values (%s, %s, %s, %s, %s, %s, %s, %s) returning *""",
        (user_id, kind, title, subtitle or "", signed, balance, used, reference or demo_reference()))
    tx = await cur.fetchone()
    return {"userId": user_id, "balanceMinor": balance, "fulizaUsedMinor": used, "fulizaLimitMinor": limit,
            "tx": tx_shape(tx)}


async def move_now(user_id: str, **kw) -> dict:
    """move() in a transaction of its own, followed by its text."""
    async with db.tx() as conn:
        moved = await move(conn, user_id, **kw)
    notify_moved(moved)
    return moved


# ------------------------------------------------------------------- admin --
async def set_wallet(user_id: str, *, pin: str | None = None, balance_minor: int | None = None,
                     fuliza_limit_minor: int | None = None, name: str | None = None, phone: str | None = None) -> dict:
    """Create the wallet (a PIN is needed then) or change any part of it. Setting
    a balance keeps the statement; reset() is the one that wipes it."""
    try:
        async with db.tx() as conn:
            cur = await conn.execute("select 1 from app.mpesa_demo_wallet where user_id = %s for update", (user_id,))
            if await cur.fetchone():
                cur = await conn.execute(
                    """update app.mpesa_demo_wallet
                          set pin = coalesce(%s, pin), balance_minor = coalesce(%s, balance_minor),
                              fuliza_limit_minor = coalesce(%s, fuliza_limit_minor),
                              holder_name = coalesce(%s, holder_name), phone = coalesce(%s, phone), updated_at = now()
                        where user_id = %s returning *""",
                    (pin, balance_minor, fuliza_limit_minor, name, phone, user_id))
            else:
                if not pin:
                    raise RailError(400, "pin_required", "Set a four digit PIN to create the wallet.")
                cur = await conn.execute(
                    """insert into app.mpesa_demo_wallet (user_id, pin, balance_minor, fuliza_limit_minor,
                                                          holder_name, phone)
                       values (%s, %s, %s, %s, %s, %s) returning *""",
                    (user_id, pin, balance_minor or 0, fuliza_limit_minor or 0, name, phone))
            return wallet_shape(await cur.fetchone())
    except UniqueViolation:
        raise RailError(409, "pin_taken", "That PIN is already assigned to another account.")


async def reset(user_id: str, balance_minor: int | None = None) -> dict:
    """Back to a chosen balance with an empty statement and nothing owed. The
    device token stays, so the handset stays linked."""
    async with db.tx() as conn:
        await conn.execute("delete from app.mpesa_demo_tx where user_id = %s", (user_id,))
        cur = await conn.execute(
            """update app.mpesa_demo_wallet set balance_minor = coalesce(%s, balance_minor), fuliza_used_minor = 0,
                      updated_at = now() where user_id = %s returning *""", (balance_minor, user_id))
        row = await cur.fetchone()
    if not row:
        raise no_wallet()
    return wallet_shape(row)


async def clear(user_id: str) -> None:
    """The wallet and its statement gone; a handset holding its token is unlinked."""
    async with db.tx() as conn:
        await conn.execute("delete from app.mpesa_demo_tx where user_id = %s", (user_id,))
        await conn.execute("delete from app.mpesa_demo_wallet where user_id = %s", (user_id,))


# ------------------------------------------------------------------- texts --
def _kes(minor: int) -> str:
    return f"Ksh{abs(minor) / 100:,.2f}"


def _stamp(at: str | None) -> str:
    when = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
    t = when.astimezone(NAIROBI)
    hour = t.hour % 12 or 12
    return f"{t.day}/{t.month}/{t.strftime('%y')} at {hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _tail() -> str:
    """The optional end every text shares, from Render: the daily limit, then the link."""
    s = get_settings()
    limit = f" Demo wallet daily limit is {_kes(int(round(s.sms_daily_limit_kes * 100)))}." \
        if s.sms_daily_limit_kes > 0 else ""
    link = (s.sms_link or "").strip()
    return limit + (f" {s.vip_merchant}: {link}" if link else "")


def message_for(tx: dict, wallet: dict, fuliza_used_minor: int = 0) -> str:
    """The confirmation text for one movement, laid out like Venti's (receipt
    code, amount, who, account, date and time, balance, transaction cost, then
    the daily limit and link set on Render). It names obisflow and the demo
    wallet, never Safaricom or M-PESA, so a text that reaches a phone is
    always plainly ours."""
    merchant = get_settings().vip_merchant
    ref, when = tx.get("reference") or demo_reference(), _stamp(tx.get("at"))
    amount, balance = _kes(tx["amountMinor"]), _kes(tx["balanceAfterMinor"])
    owed = f" Fuliza outstanding is {_kes(fuliza_used_minor)}." if fuliza_used_minor > 0 else ""
    phone = "".join(ch for ch in (wallet.get("phone") or "") if ch.isdigit())
    other = (tx.get("subtitle") or "").strip()
    bal = f"New demo wallet balance is {balance}.{owed}"
    tail = _tail()
    kind = tx["kind"]
    if kind == "DEPOSIT":
        return (f"{ref} Confirmed. {amount} sent to {merchant}{' for account ' + phone if phone else ''} on {when}. "
                f"{bal} Transaction cost, Ksh0.00.{tail}")
    if kind == "WITHDRAWAL":
        return f"{ref} Confirmed. You have received {amount} from {merchant} on {when}. {bal}{tail}"
    if kind == "RECEIVE":
        return f"{ref} Confirmed. You have received {amount} from {other or 'a sender'} on {when}. {bal}{tail}"
    if kind == "AGENT_WITHDRAWAL":
        return (f"{ref} Confirmed. You have withdrawn {amount} at {other or 'an agent'} on {when}. {bal} "
                f"Transaction cost, Ksh0.00.{tail}")
    if kind == "REVERSAL":
        return (f"{ref} Reversed. Your payment of {amount} to {merchant} could not be completed and is back "
                f"on your demo wallet on {when}. {bal}{tail}")
    return f"{ref} Confirmed. {amount} moved on your demo wallet on {when}. {bal}{tail}"


def test_message(name: str | None) -> str:
    first = (name or "").strip().split(" ")[0] or "Customer"
    merchant = get_settings().vip_merchant
    return f"{merchant} demo wallet test. Dear {first}, your demo wallet texts are set up and will arrive on this number."


def notify_moved(moved: dict) -> None:
    """The text for a movement, sent in the background once it has committed.
    It goes to the number on the wallet; a refusal is logged, never raised."""
    from . import notify

    async def go():
        try:
            w = await wallet_for(moved["userId"])
            if not w or not w["phone"]:
                return
            await notify._send_sms(moved["userId"], f"mpesa_demo_{moved['tx']['kind'].lower()}", w["phone"],
                                   message_for(moved["tx"], w, moved["fulizaUsedMinor"]))
        except Exception as e:                       # a text must never undo a movement
            log.warning("demo wallet text failed: %s", e)

    notify._later(go())
