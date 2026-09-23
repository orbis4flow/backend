"""orbisflow API.

Run locally:   uvicorn app.main:app --reload
On Render:     see render.yaml
"""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db, supabase_auth
from .services import referrals as referral_jobs, tron
from .config import get_settings
from .routers import auth, market, me, payment_methods, payments, referrals, trades, webhooks

# psycopg's async driver needs the selector loop on Windows (local dev only)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("orbisflow")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.open_pool()
    stop = asyncio.Event()
    # background jobs: USDT deposits to match, referral earnings to count and pay
    jobs = [asyncio.create_task(tron.watch(stop)), asyncio.create_task(referral_jobs.work(stop))]
    yield
    stop.set()
    for j in jobs:
        j.cancel()
    await supabase_auth.close()
    await db.close_pool()


settings = get_settings()
app = FastAPI(
    title="orbisflow API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.is_dev else None,     # no public API explorer in production
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=False,                            # auth travels in the Authorization header
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    """One readable sentence instead of pydantic's list, so the UI can show it as is."""
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "input"
    msg = first.get("msg", "is not valid").removeprefix("Value error, ")
    names = {"email": "Email", "password": "Password", "name": "Name", "amount_usd": "Amount",
             "phone": "Phone number", "referral_code": "Referral code"}
    return JSONResponse(status_code=422, content={"detail": f"{names.get(field, field.replace('_', ' ').capitalize())}: {msg}"})


@app.exception_handler(Exception)
async def unexpected(_: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on our side. Try again."})


for r in (auth.router, me.router, payment_methods.router, referrals.router, payments.router, trades.router,
          market.router, webhooks.router):
    app.include_router(r)


@app.get("/health", tags=["health"])
async def health():
    s = get_settings()
    try:
        await db.one("select 1 from app.profiles limit 1")
        database = "ok"
    except Exception as e:
        database = await db.diagnose(e)
    trading = "unknown"
    if database == "ok":
        try:
            await db.one("select 1 from app.trades limit 1")
            trading = "ok"
        except Exception:                               # sql/003_trades.sql not run yet
            trading = "run sql/003_trades.sql"
    extras = "unknown"
    if database == "ok":
        try:
            await db.one("select 1 from app.referral_earnings limit 1")
            extras = "ok"
        except Exception:                               # sql/004 not run yet
            extras = "run sql/004_notifications_usdt_referrals.sql"
    return {"ok": database == "ok", "database": database, "trading": trading, "notifications_usdt_referrals": extras,
            "payhero": s.payhero_ready, "paystack": s.paystack_ready,
            "email": s.email_ready, "sms": s.sms_ready, "news": bool(s.finnhub_api_key)}
