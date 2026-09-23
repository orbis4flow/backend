"""Who is calling: verifies the Supabase access token on every request.

Supabase signs access tokens either with asymmetric keys (published at the
project's JWKS endpoint) or, on older projects, with a shared HS256 secret.
Both are verified locally. If neither is possible (HS256 with no secret
configured), the token is checked with Supabase itself and cached briefly.
"""
import hashlib
import time
from dataclasses import dataclass, field

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from . import supabase_auth
from .config import get_settings


@dataclass
class AuthUser:
    id: str
    email: str
    token: str
    meta: dict = field(default_factory=dict)


_jwks: jwt.PyJWKClient | None = None
_remote_cache: dict[str, tuple[float, AuthUser]] = {}
_REMOTE_TTL = 60.0


def _jwks_client() -> jwt.PyJWKClient:
    global _jwks
    if _jwks is None:
        s = get_settings()
        _jwks = jwt.PyJWKClient(
            f"{s.supabase_url}/auth/v1/.well-known/jwks.json",
            headers={"apikey": s.supabase_publishable_key},
            cache_keys=True,
            lifespan=3600,
        )
    return _jwks


def _from_claims(claims: dict, token: str) -> AuthUser:
    return AuthUser(id=claims["sub"], email=claims.get("email") or "", token=token,
                    meta=claims.get("user_metadata") or {})


async def verify(token: str) -> AuthUser:
    s = get_settings()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Your session has ended. Log in again.")
    alg = header.get("alg", "")

    try:
        if alg in ("RS256", "ES256", "EdDSA"):
            key = await run_in_threadpool(_jwks_client().get_signing_key_from_jwt, token)
            claims = jwt.decode(token, key.key, algorithms=[alg], audience="authenticated")
            return _from_claims(claims, token)
        if alg == "HS256" and s.supabase_jwt_secret:
            claims = jwt.decode(token, s.supabase_jwt_secret, algorithms=["HS256"], audience="authenticated")
            return _from_claims(claims, token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Your session has ended. Log in again.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Your session has ended. Log in again.")

    # no local way to check it: ask Supabase, and remember the answer for a minute
    digest = hashlib.sha256(token.encode()).hexdigest()
    hit = _remote_cache.get(digest)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        u = await supabase_auth.get_user(token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Your session has ended. Log in again.")
    user = AuthUser(id=u["id"], email=u.get("email") or "", token=token, meta=u.get("user_metadata") or {})
    if len(_remote_cache) > 5000:
        _remote_cache.clear()
    _remote_cache[digest] = (time.monotonic() + _REMOTE_TTL, user)
    return user


def bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Log in to continue.")
    return auth[7:].strip()


async def current_user(token: str = Depends(bearer)) -> AuthUser:
    return await verify(token)
