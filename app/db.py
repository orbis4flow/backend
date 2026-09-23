"""Postgres (Supabase) access through one async connection pool.

Tables live in the `app` schema, created by hand from sql/001_schema.sql.
"""
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import get_settings

_pool: AsyncConnectionPool | None = None


async def open_pool() -> None:
    global _pool
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    _pool = AsyncConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=10,
        open=False,
        # prepare_threshold=None keeps this working behind Supabase's poolers,
        # which do not carry prepared statements between connections
        kwargs={"row_factory": dict_row, "prepare_threshold": None},
    )
    await _pool.open(wait=False)


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool is not open")
    return _pool


@asynccontextmanager
async def tx() -> AsyncIterator[AsyncConnection]:
    """One connection, one transaction: commits on success, rolls back on error."""
    async with pool().connection() as conn:
        async with conn.transaction():
            yield conn


async def one(sql: str, params: Any = None, conn: AsyncConnection | None = None) -> dict | None:
    if conn is not None:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()
    async with tx() as c:
        cur = await c.execute(sql, params)
        return await cur.fetchone()


async def many(sql: str, params: Any = None, conn: AsyncConnection | None = None) -> list[dict]:
    if conn is not None:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()
    async with tx() as c:
        cur = await c.execute(sql, params)
        return await cur.fetchall()


async def run(sql: str, params: Any = None, conn: AsyncConnection | None = None) -> int:
    if conn is not None:
        cur = await conn.execute(sql, params)
        return cur.rowcount
    async with tx() as c:
        cur = await c.execute(sql, params)
        return cur.rowcount


async def diagnose(err: Exception) -> str:
    """Why the database is unreachable, in words, without the address or password.
    A pool timeout hides the cause, so one direct connection is tried to find it."""
    from psycopg import errors

    if isinstance(err, errors.UndefinedTable) or "does not exist" in str(err):
        return "connected, but the app tables are missing: run sql/001_schema.sql"
    try:
        conn = await AsyncConnection.connect(get_settings().database_url, connect_timeout=10)
        await conn.close()
        return f"reachable now, earlier error: {type(err).__name__}"
    except Exception as e:
        m = str(e).lower()
        for needle, why in (
            ("password authentication failed", "password rejected: check the password in DATABASE_URL"),
            ("tenant or user not found", "pooler user not found: the user must be postgres.<project-ref>"),
            ("failed to resolve host", "host name not found: if the password has @ # / : ? or %, percent-encode it "
                                       "(@ is %40) or reset it to letters and numbers"),
            ("could not translate host name", "host name not found: check the host in DATABASE_URL"),
            ("network is unreachable", "host unreachable: use the Session pooler string, not the direct db. host"),
            ("timeout", "connection timed out: use the Session pooler string, not the direct db. host"),
            ("invalid dsn", "DATABASE_URL is not a valid connection string (encode special characters in the password)"),
            ("missing", "DATABASE_URL is not a valid connection string"),
        ):
            if needle in m:
                return "error: " + why
        return f"error: {type(e).__name__}: {_scrub(str(e))}"


def _scrub(msg: str) -> str:
    """The driver's own words, with anything that locates or unlocks the database removed."""
    import re
    msg = re.sub(r"postgres(ql)?://\S+", "<url>", msg)
    msg = re.sub(r'"[^"]*"', '"<hidden>"', msg)                      # anything quoted can hold a host or a
    msg = re.sub(r"'[^']*'", "'<hidden>'", msg)                      # piece of a mistyped password
    msg = re.sub(r"\S*@\S*", "<hidden>", msg)
    msg = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "<ip>", msg)
    msg = re.sub(r"\b[0-9a-f]{1,4}(:[0-9a-f]{0,4}){2,7}\b", "<ip>", msg)   # IPv6
    msg = re.sub(r"[\w.-]+\.(supabase\.(co|com)|amazonaws\.com)", "<host>", msg)
    return " ".join(msg.split())[:240]
