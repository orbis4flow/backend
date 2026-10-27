"""A log of the calls that matter, for the admin site's Logs page.

One row per M-Pesa prompt, card checkout, payment result, provider callback,
sign-up, Google sign-in, new account, sign-in and price-feed drop, with
whether it worked and, if not, why (sql/010_events.sql). Emails and SMS are
logged on their own, in app.notifications.

Recording never fails the request it describes: before 010 is run, or with
the database down, it does nothing.
"""
import logging
import random

from .. import db

log = logging.getLogger("obisflow.events")

KEEP_DAYS = 90


async def record(kind: str, ok: bool, *, detail: str | None = None, provider: str | None = None,
                 ref: str | None = None, user_id: str | None = None, ms: int | None = None) -> None:
    try:
        await db.run(
            """insert into app.events (kind, ok, provider, ref, user_id, detail, ms)
               values (%s, %s, %s, %s, %s, %s, %s)""",
            (kind, ok, provider, ref, user_id, (detail or None) and str(detail)[:500], ms))
        if random.random() < 0.01:                    # now and then, drop what is past keeping
            await db.run("delete from app.events where created_at < now() - make_interval(days => %s)", (KEEP_DAYS,))
    except Exception as e:
        log.debug("event %s not recorded: %s", kind, e)
