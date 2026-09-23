"""Sessions and the two-factor gate, checked on every signed-in request.

Each Supabase session (the session_id claim in its tokens) gets a row: the
device, the IP, when it was last seen, whether it has passed two-factor and
whether it has been signed out from the security page. A revoked session is
refused. With two-factor on, a session that has not entered its code is
refused with 428 everywhere except the endpoints that send and check it.

Looked up at most once a minute per session, from memory in between; the
changes made here clear that memory at once.
"""
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from psycopg.errors import UndefinedTable

from .. import db

_seen: dict[str, tuple[float, dict]] = {}
TTL = 60.0


def forget(session_id: str | None = None) -> None:
    if session_id is None:
        _seen.clear()
    else:
        _seen.pop(session_id, None)


def device_of(ua: str) -> str:
    ua = ua or ""
    browser = ("Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else "Chrome" if "Chrome/" in ua
               else "Safari" if "Safari/" in ua else "Firefox" if "Firefox/" in ua else "Browser")
    os_ = ("Windows" if "Windows" in ua else "Android" if "Android" in ua
           else "iOS" if ("iPhone" in ua or "iPad" in ua) else "macOS" if "Mac OS X" in ua
           else "Linux" if "Linux" in ua else "")
    return f"{browser} · {os_}" if os_ else browser


def ip_of(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or None) if fwd else (request.client.host if request.client else None)


async def check(user_id: str, session_id: str | None, request: Request, allow_pending: bool = False) -> None:
    """Record the session, and refuse it if revoked or still owing a code."""
    if not session_id:
        return
    hit = _seen.get(session_id)
    if hit and hit[0] > time.monotonic():
        state = hit[1]
    else:
        try:
            state = await _load(user_id, session_id, request)
        except UndefinedTable:                                  # sql/005 not run yet
            return
        _seen[session_id] = (time.monotonic() + TTL, state)
    if state["revoked"]:
        raise HTTPException(status_code=401, detail="This device was signed out. Log in again.")
    if state["twofa"] and not state["verified"] and not allow_pending:
        raise HTTPException(status_code=428, detail="Enter the code we sent you to finish signing in.",
                            headers={"X-Orbis-Otp": "login"})


async def _load(user_id: str, session_id: str, request: Request) -> dict:
    prefs = await db.one("select twofa_enabled, login_alerts from app.preferences where user_id = %s", (user_id,))
    twofa = bool(prefs and prefs["twofa_enabled"])
    device, ip = device_of(request.headers.get("user-agent", "")), ip_of(request)
    row = await db.one(
        """insert into app.sessions (id, user_id, device, ip, mfa_verified)
           values (%s, %s, %s, %s, %s)
           on conflict (id) do update set last_seen_at = now(), ip = excluded.ip
        returning *, (xmax = 0) as created""",
        (session_id, user_id, device, ip, not twofa))
    if row["created"] and (prefs is None or prefs["login_alerts"]):
        # a new device; not for the session a brand-new account starts with
        p = await db.one("select created_at from app.profiles where id = %s", (user_id,))
        if p and datetime.now(timezone.utc) - p["created_at"] > timedelta(minutes=5):
            from . import notify
            notify._later(notify.login_alert(user_id, device, ip))
    return {"revoked": row["revoked_at"] is not None, "twofa": twofa, "verified": row["mfa_verified"]}


async def mark_verified(session_id: str) -> None:
    await db.run("update app.sessions set mfa_verified = true where id = %s", (session_id,))
    forget(session_id)
