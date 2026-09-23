"""Profiles, their two accounts, and their referral code."""
from decimal import Decimal

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from .. import db
from ..config import get_settings
from ..security import AuthUser
from ..util import referral_code


def _clean_code(value: str | None) -> str | None:
    v = (value or "").strip().upper()
    return v or None


async def referrer_id(code: str | None, conn: AsyncConnection | None = None) -> str | None:
    code = _clean_code(code)
    if not code:
        return None
    row = await db.one("select id from app.profiles where referral_code = %s", (code,), conn=conn)
    return str(row["id"]) if row else None


async def ensure(user_id: str, email: str, full_name: str | None = None,
                 ref_code: str | None = None) -> dict:
    """The user's profile, created on first sight with its accounts and code."""
    row = await db.one("select * from app.profiles where id = %s", (user_id,))
    if row:
        return row

    s = get_settings()
    async with db.tx() as conn:
        ref = await referrer_id(ref_code, conn)
        if ref == user_id:
            ref = None
        created = False
        for _ in range(8):
            try:
                async with conn.transaction():      # a savepoint, so a clash can retry
                    ins = await conn.execute(
                        """insert into app.profiles (id, email, full_name, referral_code, referred_by)
                           values (%s, %s, %s, %s, %s)
                           on conflict (id) do nothing""",
                        (user_id, email, (full_name or "").strip() or None, referral_code(), ref),
                    )
                    created = ins.rowcount > 0
                break
            except UniqueViolation:
                continue                             # the random code was taken, draw again
        else:
            raise RuntimeError("could not allocate a referral code")

        await conn.execute(
            """insert into app.accounts (user_id, kind, balance) values
                 (%s, 'demo', %s), (%s, 'real', 0)
               on conflict (user_id, kind) do nothing""",
            (user_id, Decimal(str(s.demo_balance_usd)), user_id),
        )
        cur = await conn.execute("select * from app.profiles where id = %s", (user_id,))
        row = await cur.fetchone()
    if created:                                     # a new account gets its welcome email, once
        from . import notify
        notify._later(notify.welcome(user_id))
    return row


async def for_user(user: AuthUser) -> dict:
    """The profile behind a verified token, creating it if this is the first call
    (a Google sign-in, or a sign-up that needed email confirmation)."""
    return await ensure(user.id, user.email, user.meta.get("full_name") or user.meta.get("name"),
                        user.meta.get("referral_code"))


def public(p: dict, account_id: str | None = None) -> dict:
    """The profile as the UI reads it."""
    s = get_settings()
    return {
        "id": str(p["id"]),
        "email": p["email"],
        "name": p["full_name"] or "",
        "phone": p["phone"] or "",
        "country": {"code": p["country_code"], "name": p["country_name"]} if p["country_code"] else None,
        "currency": p["currency"],
        "referralCode": p["referral_code"],
        "referralLink": f"{s.frontend_url}/r/{p['referral_code']}",
        "accountId": account_id,
        "createdAt": p["created_at"].isoformat(),
    }


async def accounts(user_id: str) -> list[dict]:
    rows = await db.many(
        "select id, kind, currency, balance from app.accounts where user_id = %s order by kind", (user_id,))
    label = {"demo": "Demo account", "real": "Real account"}
    return [{"id": str(r["id"]), "kind": r["kind"], "label": label[r["kind"]],
             "currency": r["currency"], "balance": float(r["balance"])} for r in rows]


async def real_account_id(user_id: str, conn: AsyncConnection | None = None) -> str:
    row = await db.one("select id from app.accounts where user_id = %s and kind = 'real'", (user_id,), conn=conn)
    if not row:
        raise RuntimeError("real account missing")
    return str(row["id"])
