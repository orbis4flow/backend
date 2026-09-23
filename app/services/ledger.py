"""Money moves here and nowhere else.

Every balance change is one SQL statement that moves a transaction out of an
open state and the balance with it, so a callback that arrives twice, or a
callback racing a status poll, can only ever settle a transaction once.
"""
from decimal import Decimal

from psycopg import AsyncConnection

from .. import db

OPEN = ("pending", "processing")


async def complete_deposit(reference: str, *, receipt: str | None = None,
                           provider_ref: str | None = None) -> dict | None:
    """Credit a deposit. Returns the new balance, or None if it was already settled."""
    return await db.one(
        """with t as (
             update app.transactions
                set status = 'completed', completed_at = now(),
                    provider_receipt = coalesce(%(receipt)s, provider_receipt),
                    provider_ref = coalesce(%(pref)s, provider_ref)
              where reference = %(ref)s and type = 'deposit' and status in ('pending', 'processing')
          returning account_id, net_usd, user_id, payment_method_id
           )
           update app.accounts a set balance = a.balance + t.net_usd
             from t where a.id = t.account_id
         returning a.balance, t.user_id, t.payment_method_id""",
        {"receipt": receipt, "pref": provider_ref, "ref": reference},
    )


async def fail_deposit(reference: str, reason: str | None) -> bool:
    n = await db.run(
        """update app.transactions set status = 'failed', failure_reason = %s, completed_at = now()
            where reference = %s and type = 'deposit' and status in ('pending', 'processing')""",
        ((reason or "Payment was not completed")[:300], reference),
    )
    return n > 0


async def debit_for_withdrawal(conn: AsyncConnection, user_id: str, total: Decimal) -> str | None:
    """Take amount + fee from the real balance, only if it is all there.
    Returns the account id, or None when the balance is short."""
    cur = await conn.execute(
        """update app.accounts set balance = balance - %s
            where user_id = %s and kind = 'real' and balance >= %s
        returning id""",
        (total, user_id, total),
    )
    row = await cur.fetchone()
    return str(row["id"]) if row else None


async def verify_method(method_id) -> None:
    """A method that has just paid us successfully belongs to its owner."""
    if method_id:
        await db.run("update app.payment_methods set status = 'verified' where id = %s and status = 'pending'",
                     (method_id,))
