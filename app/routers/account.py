"""Preferences, security and devices: what the Preferences and Security pages save.

Responsible-trading limits are enforced where money moves (services/limits.py):
a daily deposit cap, a daily loss limit on real-money trades, a cooling-off
pause and self-exclusion. A pause or an exclusion cannot be lifted early.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, supabase_auth
from ..security import AuthUser, current_user
from ..services import notify, otp, profiles, sessions

router = APIRouter(tags=["preferences and security"])

DURATIONS = ("1m", "5m", "15m", "1h")


async def prefs_of(user_id: str) -> dict:
    """The user's preferences, created with the defaults on first read."""
    row = await db.one("select * from app.preferences where user_id = %s", (user_id,))
    if row:
        return row
    await db.run("insert into app.preferences (user_id) values (%s) on conflict do nothing", (user_id,))
    return await db.one("select * from app.preferences where user_id = %s", (user_id,))


def _iso(v):
    return v.isoformat() if v else None


def public(p: dict) -> dict:
    now = datetime.now(timezone.utc)
    active = lambda t: t if t and t > now else None
    return {
        "defaultStake": float(p["default_stake"]), "defaultDuration": p["default_duration"],
        "confirmTrades": p["confirm_trades"],
        "settlementNotifications": p["settlement_notifications"], "marketingEmails": p["marketing_emails"],
        "loginAlerts": p["login_alerts"], "withdrawalConfirm": p["withdrawal_confirm"],
        "twofa": {"enabled": p["twofa_enabled"], "channel": p["twofa_channel"]},
        "passwordChangedAt": _iso(p["password_changed_at"]),
        "limits": {
            "depositDaily": float(p["deposit_limit_daily"]) if p["deposit_limit_daily"] is not None else None,
            "lossDaily": float(p["loss_limit_daily"]) if p["loss_limit_daily"] is not None else None,
            "coolingOffUntil": _iso(active(p["cooling_off_until"])),
            "selfExcludedUntil": _iso(active(p["self_excluded_until"])),
        },
    }


# ------------------------------------------------------------- preferences --
class PrefsPatch(BaseModel):
    default_stake: float | None = Field(default=None, gt=0, le=10000)
    default_duration: Literal["1m", "5m", "15m", "1h"] | None = None
    confirm_trades: bool | None = None
    settlement_notifications: bool | None = None
    marketing_emails: bool | None = None
    login_alerts: bool | None = None


@router.get("/preferences")
async def get_prefs(user: AuthUser = Depends(current_user)):
    await profiles.for_user(user)
    return public(await prefs_of(user.id))


@router.patch("/preferences")
async def patch_prefs(body: PrefsPatch, user: AuthUser = Depends(current_user)):
    await profiles.for_user(user)
    await prefs_of(user.id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "default_stake" in fields:
        fields["default_stake"] = Decimal(str(fields["default_stake"])).quantize(Decimal("0.01"))
    if fields:
        sets = ", ".join(f"{k} = %({k})s" for k in fields)        # keys are the model's own names
        fields["uid"] = user.id
        await db.run(f"update app.preferences set {sets} where user_id = %(uid)s", fields)
    return public(await prefs_of(user.id))


class Limits(BaseModel):
    deposit_daily: float | None = Field(default=None, gt=0, le=1_000_000)   # null removes the cap
    loss_daily: float | None = Field(default=None, gt=0, le=1_000_000)


@router.put("/preferences/limits")
async def set_limits(body: Limits, user: AuthUser = Depends(current_user)):
    await prefs_of(user.id)
    await db.run("update app.preferences set deposit_limit_daily = %s, loss_limit_daily = %s where user_id = %s",
                 (body.deposit_daily, body.loss_daily, user.id))
    return public(await prefs_of(user.id))


class Pause(BaseModel):
    kind: Literal["cooling_off", "self_exclusion"]
    days: int = Field(ge=1, le=3650)


@router.post("/preferences/pause")
async def pause(body: Pause, user: AuthUser = Depends(current_user)):
    """Cooling-off: 1 to 42 days without trading. Self-exclusion: 6 months or
    more, no trading and no deposits. Neither can be shortened or lifted."""
    if body.kind == "cooling_off" and body.days > 42:
        raise HTTPException(status_code=400, detail="A cooling-off pause is 1 to 42 days. For longer, use self-exclusion.")
    if body.kind == "self_exclusion" and body.days < 180:
        raise HTTPException(status_code=400, detail="Self-exclusion is at least six months.")
    p = await prefs_of(user.id)
    col = "cooling_off_until" if body.kind == "cooling_off" else "self_excluded_until"
    until = datetime.now(timezone.utc) + timedelta(days=body.days)
    if p[col] and p[col] > until:                      # never shortened
        until = p[col]
    await db.run(f"update app.preferences set {col} = %s where user_id = %s", (until, user.id))
    return public(await prefs_of(user.id))


# ----------------------------------------------------------------- password --
class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)


@router.post("/security/password", status_code=204)
async def change_password(body: PasswordChange, user: AuthUser = Depends(current_user)):
    try:
        await supabase_auth.sign_in(user.email, body.current_password)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Your current password is not right.")
    await supabase_auth.update_password(user.token, body.new_password)
    await prefs_of(user.id)
    await db.run("update app.preferences set password_changed_at = now() where user_id = %s", (user.id,))
    await _sign_out_others(user)
    notify._later(notify.password_changed(user.id))


# --------------------------------------------------------------- two-factor --
class TwoFaStart(BaseModel):
    channel: Literal["email", "sms"]


class CodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


@router.post("/security/2fa/start")
async def twofa_start(body: TwoFaStart, user: AuthUser = Depends(current_user)):
    """Send a code to the channel being turned on, so it is known to work."""
    p = await prefs_of(user.id)
    if p["twofa_enabled"]:
        raise HTTPException(status_code=400, detail="Two-factor sign-in is already on.")
    await db.run("update app.preferences set twofa_channel = %s where user_id = %s", (body.channel, user.id))
    return await otp.issue(user.id, "enable_2fa", body.channel)


@router.post("/security/2fa/enable")
async def twofa_enable(body: CodeIn, user: AuthUser = Depends(current_user)):
    p = await prefs_of(user.id)
    await otp.check(user.id, "enable_2fa", body.code)
    await db.run("update app.preferences set twofa_enabled = true where user_id = %s", (user.id,))
    # this device has just proved itself; every other session already signed in stays signed in
    if user.session_id:
        await sessions.mark_verified(user.session_id)
    sessions.forget()
    notify._later(notify.twofa_changed(user.id, True, p["twofa_channel"]))
    return public(await prefs_of(user.id))


@router.post("/security/2fa/disable/start")
async def twofa_disable_start(user: AuthUser = Depends(current_user)):
    p = await prefs_of(user.id)
    if not p["twofa_enabled"]:
        raise HTTPException(status_code=400, detail="Two-factor sign-in is not on.")
    return await otp.issue(user.id, "disable_2fa", p["twofa_channel"] or "email")


@router.post("/security/2fa/disable")
async def twofa_disable(body: CodeIn, user: AuthUser = Depends(current_user)):
    p = await prefs_of(user.id)
    await otp.check(user.id, "disable_2fa", body.code)
    await db.run("update app.preferences set twofa_enabled = false where user_id = %s", (user.id,))
    sessions.forget()
    notify._later(notify.twofa_changed(user.id, False, p["twofa_channel"]))
    return public(await prefs_of(user.id))


class Toggle(BaseModel):
    enabled: bool


@router.put("/security/withdrawal-confirm")
async def withdrawal_confirm(body: Toggle, user: AuthUser = Depends(current_user)):
    await prefs_of(user.id)
    await db.run("update app.preferences set withdrawal_confirm = %s where user_id = %s", (body.enabled, user.id))
    return public(await prefs_of(user.id))


# ----------------------------------------------------------------- devices --
def _ago(t: datetime) -> str:
    s = int((datetime.now(timezone.utc) - t).total_seconds())
    return ("active now" if s < 120 else f"{s // 60} min ago" if s < 3600
            else f"{s // 3600} h ago" if s < 86400 else f"{s // 86400} d ago")


@router.get("/sessions")
async def list_sessions(user: AuthUser = Depends(current_user)):
    rows = await db.many(
        """select * from app.sessions where user_id = %s and revoked_at is null
              and last_seen_at > now() - interval '30 days'
            order by last_seen_at desc""", (user.id,))
    return [{"id": r["id"], "device": r["device"] or "Unknown device", "place": r["ip"] or "Unknown",
             "lastSeen": _ago(r["last_seen_at"]), "current": r["id"] == user.session_id} for r in rows]


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke(session_id: str, user: AuthUser = Depends(current_user)):
    if session_id == user.session_id:
        raise HTTPException(status_code=400, detail="That is this device. Use Log out instead.")
    n = await db.run("update app.sessions set revoked_at = now() where id = %s and user_id = %s "
                     "and revoked_at is null", (session_id, user.id))
    if not n:
        raise HTTPException(status_code=404, detail="That session was not found.")
    sessions.forget(session_id)


async def _sign_out_others(user: AuthUser) -> None:
    try:
        await supabase_auth.sign_out(user.token, scope="others")   # their refresh tokens stop working
    except Exception:
        pass
    await db.run("update app.sessions set revoked_at = now() where user_id = %s and id <> %s "
                 "and revoked_at is null", (user.id, user.session_id or ""))
    sessions.forget()


@router.post("/sessions/revoke-others", status_code=204)
async def revoke_others(user: AuthUser = Depends(current_user)):
    await _sign_out_others(user)
