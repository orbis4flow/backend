"""Account creation and sign-in, relayed to Supabase Auth."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from .. import supabase_auth
from ..config import get_settings
from ..security import AuthUser, bearer, current_user
from ..services import profiles

router = APIRouter(prefix="/auth", tags=["auth"])


class SignUp(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    referral_code: str | None = Field(default=None, max_length=20)


class SignIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class Refresh(BaseModel):
    refresh_token: str


class Forgot(BaseModel):
    email: EmailStr


class NewPassword(BaseModel):
    password: str = Field(min_length=8, max_length=72)


async def _session(s: dict) -> dict:
    """A Supabase session plus our profile, the shape the UI stores."""
    u = s["user"]
    meta = u.get("user_metadata") or {}
    p = await profiles.ensure(u["id"], u.get("email") or "", meta.get("full_name") or meta.get("name"),
                              meta.get("referral_code"))
    account = await profiles.real_account_id(str(p["id"]))
    return {
        "access_token": s["access_token"],
        "refresh_token": s["refresh_token"],
        "expires_at": s.get("expires_at"),
        "profile": profiles.public(p, account),
    }


@router.post("/signup")
async def sign_up(body: SignUp):
    s = get_settings()
    code = (body.referral_code or "").strip().upper() or None
    if code and not await profiles.referrer_id(code):
        raise HTTPException(status_code=400, detail="That referral code does not exist. Check it, or leave it empty.")

    res = await supabase_auth.sign_up(
        body.email, body.password,
        {"full_name": body.name.strip(), "referral_code": code},
        redirect_to=f"{s.frontend_url}/login?confirmed=1",
    )

    if res.get("access_token"):                       # email confirmation is off
        return await _session(res)

    user = res.get("user") or res
    # Supabase answers an existing email with a user that has no identities
    if not user.get("identities"):
        raise HTTPException(status_code=409, detail="An account with this email already exists. Log in instead.")
    await profiles.ensure(user["id"], user.get("email") or body.email, body.name, code)
    return {"confirm_email": True, "email": body.email}


@router.post("/login")
async def sign_in(body: SignIn):
    return await _session(await supabase_auth.sign_in(body.email, body.password))


@router.post("/refresh")
async def refresh(body: Refresh):
    return await _session(await supabase_auth.refresh(body.refresh_token))


@router.post("/logout", status_code=204)
async def sign_out(token: str = Depends(bearer)):
    await supabase_auth.sign_out(token)


@router.post("/password/forgot", status_code=204)
async def forgot(body: Forgot):
    s = get_settings()
    try:
        await supabase_auth.recover(body.email, f"{s.frontend_url}/login?reset=1")
    except HTTPException as e:
        if e.status_code == 429:
            raise
    # the same answer whether or not the email has an account


@router.post("/password/update", status_code=204)
async def update_password(body: NewPassword, user: AuthUser = Depends(current_user)):
    await supabase_auth.update_password(user.token, body.password)


@router.get("/google")
async def google(ref: str | None = None):
    """Where to send the browser for Google sign-in. Supabase sends it back to
    /login with the session in the URL fragment."""
    if not await supabase_auth.provider_enabled("google"):
        raise HTTPException(status_code=503, detail="Google sign-in is not switched on yet. Use your email for now.")
    s = get_settings()
    back = f"{s.frontend_url}/login?oauth=google" + (f"&ref={ref.strip().upper()}" if ref else "")
    return {"url": supabase_auth.oauth_url("google", back)}
