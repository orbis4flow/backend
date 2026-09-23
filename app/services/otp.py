"""One-time codes: six digits, by email or SMS, for signing in with two-factor,
turning it on or off, and confirming a withdrawal.

Only a salted hash is stored. A code lives ten minutes and dies after five
wrong tries; a new one cannot be asked for within 45 seconds of the last,
or more than six times in an hour.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .. import db
from . import notify

TTL = timedelta(minutes=10)
MAX_TRIES = 5
GAP = timedelta(seconds=45)
PER_HOUR = 6

WHAT = {
    "login": "finishes signing you in",
    "enable_2fa": "turns on two-factor sign-in",
    "disable_2fa": "turns off two-factor sign-in",
    "withdrawal": "confirms your withdrawal",
}


def _hash(salt: str, code: str) -> str:
    return hashlib.sha256((salt + code).encode()).hexdigest()


def mask(channel: str, to: str) -> str:
    if channel == "sms":
        return to[:4] + "•••" + to[-3:]
    name, _, domain = to.partition("@")
    return name[:2] + "•••@" + domain


async def destination(user_id: str, channel: str) -> str:
    p = await db.one("select email, phone from app.profiles where id = %s", (user_id,))
    to = (p or {}).get("phone" if channel == "sms" else "email")
    if not to:
        raise HTTPException(status_code=400, detail="Add a phone number to your profile first." if channel == "sms"
                            else "There is no email on this account.")
    return to


async def issue(user_id: str, purpose: str, channel: str, session_id: str | None = None) -> dict:
    """Make a code, send it, and say where it went."""
    now = datetime.now(timezone.utc)
    recent = await db.many(
        "select created_at from app.otp_codes where user_id = %s and created_at > %s order by created_at desc",
        (user_id, now - timedelta(hours=1)))
    if recent and now - recent[0]["created_at"] < GAP:
        raise HTTPException(status_code=429, detail="A code was just sent. Wait a moment before asking for another.")
    if len(recent) >= PER_HOUR:
        raise HTTPException(status_code=429, detail="Too many codes this hour. Try again later.")

    to = await destination(user_id, channel)
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    # an older unused code for the same thing stops working when a new one is sent
    await db.run("update app.otp_codes set consumed_at = now() where user_id = %s and purpose = %s "
                 "and consumed_at is null", (user_id, purpose))
    await db.run(
        """insert into app.otp_codes (user_id, purpose, channel, code_hash, salt, session_id, expires_at)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (user_id, purpose, channel, _hash(salt, code), salt, session_id, now + TTL))
    if not await notify.code_now(user_id, channel, to, code, WHAT[purpose]):
        await db.run("update app.otp_codes set consumed_at = now() where user_id = %s and purpose = %s "
                     "and consumed_at is null", (user_id, purpose))
        raise HTTPException(status_code=503, detail=(
            "Text messages cannot be sent right now." if channel == "sms"
            else "Emails cannot be sent right now.") + " Try again shortly, or contact support.")
    return {"channel": channel, "to": mask(channel, to), "expiresIn": int(TTL.total_seconds())}


async def check(user_id: str, purpose: str, code: str, session_id: str | None = None) -> None:
    """Accept the code or raise. A code is good once."""
    row = await db.one(
        """select * from app.otp_codes
            where user_id = %s and purpose = %s and consumed_at is null and expires_at > now()
              and (%s::text is null or session_id = %s)
            order by created_at desc limit 1""",
        (user_id, purpose, session_id, session_id))
    if not row or row["attempts"] >= MAX_TRIES:
        raise HTTPException(status_code=400, detail="That code has expired. Ask for a new one.")
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not hmac.compare_digest(_hash(row["salt"], code), row["code_hash"]):
        await db.run("update app.otp_codes set attempts = attempts + 1 where id = %s", (row["id"],))
        left = MAX_TRIES - row["attempts"] - 1
        raise HTTPException(status_code=400, detail=f"That code is not right. {left} tr{'y' if left == 1 else 'ies'} left."
                            if left > 0 else "That code is not right, and it has now expired. Ask for a new one.")
    await db.run("update app.otp_codes set consumed_at = now() where id = %s", (row["id"],))
