"""A thin client for Supabase Auth (GoTrue), called with the publishable key.

Sign-up, sign-in, refresh, sign-out and password recovery all happen in
Supabase; this backend only relays them and keeps its own profile rows.
"""
from typing import Any

import httpx
from fastapi import HTTPException

from .config import get_settings

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = httpx.AsyncClient(
            base_url=f"{s.supabase_url}/auth/v1",
            headers={"apikey": s.supabase_publishable_key, "Content-Type": "application/json"},
            timeout=15.0,
        )
    return _client


async def close() -> None:
    if _client is not None:
        await _client.aclose()


# Supabase's own wording is written for developers; these are for traders
_FRIENDLY = {
    "invalid_credentials": "That email and password do not match.",
    "email_not_confirmed": "Confirm your email first. We sent you a link when you signed up.",
    "user_already_exists": "An account with this email already exists. Log in instead.",
    "email_exists": "An account with this email already exists. Log in instead.",
    "weak_password": "Choose a stronger password: at least 8 characters, not a common one.",
    "over_email_send_rate_limit": "Too many emails sent. Wait a minute and try again.",
    "over_request_rate_limit": "Too many attempts. Wait a minute and try again.",
    "signup_disabled": "Sign-ups are paused right now.",
    "email_address_invalid": "That email address does not look right.",
    "same_password": "Your new password must be different from the old one.",
    "refresh_token_not_found": "Your session has ended. Log in again.",
    "refresh_token_already_used": "Your session has ended. Log in again.",
    "session_not_found": "Your session has ended. Log in again.",
    "bad_jwt": "Your session has ended. Log in again.",
}


def _raise(r: httpx.Response) -> None:
    try:
        body = r.json()
    except ValueError:
        body = {}
    code = body.get("error_code") or body.get("code") or body.get("error") or ""
    msg = _FRIENDLY.get(str(code)) or body.get("msg") or body.get("error_description") or body.get("message")
    status = r.status_code if r.status_code in (400, 401, 403, 404, 409, 422, 429) else 502
    if code in ("user_already_exists", "email_exists"):
        status = 409
    raise HTTPException(status_code=status, detail=msg or "Sign-in service error. Try again.")


async def _call(method: str, path: str, *, token: str | None = None, **kw) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        r = await client().request(method, path, headers=headers, **kw)
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="The sign-in service is not reachable. Try again.")
    if r.status_code >= 400:
        _raise(r)
    return r.json() if r.content else None


async def sign_up(email: str, password: str, data: dict, redirect_to: str) -> dict:
    return await _call("POST", "/signup", params={"redirect_to": redirect_to},
                       json={"email": email, "password": password, "data": data})


async def sign_in(email: str, password: str) -> dict:
    return await _call("POST", "/token", params={"grant_type": "password"},
                       json={"email": email, "password": password})


async def refresh(refresh_token: str) -> dict:
    return await _call("POST", "/token", params={"grant_type": "refresh_token"},
                       json={"refresh_token": refresh_token})


async def sign_out(token: str, scope: str = "local") -> None:
    """scope: local (this session), others (every other session), global (all)."""
    try:
        await _call("POST", "/logout", token=token, params={"scope": scope})
    except HTTPException:
        pass  # an already-dead session is still signed out


async def recover(email: str, redirect_to: str) -> None:
    await _call("POST", "/recover", params={"redirect_to": redirect_to}, json={"email": email})


async def update_password(token: str, password: str) -> dict:
    return await _call("PUT", "/user", token=token, json={"password": password})


async def get_user(token: str) -> dict:
    return await _call("GET", "/user", token=token)


_providers: tuple[float, dict] | None = None


async def provider_enabled(provider: str) -> bool:
    """Whether a sign-in provider is switched on in Supabase (checked once a minute)."""
    global _providers
    import time
    if _providers is None or _providers[0] < time.monotonic():
        try:
            r = await client().get("/settings")
            _providers = (time.monotonic() + 60, (r.json() or {}).get("external") or {})
        except (httpx.HTTPError, ValueError):
            return True          # cannot tell: let Supabase answer for itself
    return bool(_providers[1].get(provider))


def oauth_url(provider: str, redirect_to: str) -> str:
    s = get_settings()
    return str(httpx.URL(f"{s.supabase_url}/auth/v1/authorize",
                         params={"provider": provider, "redirect_to": redirect_to}))
