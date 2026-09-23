"""Small pure helpers: money, phone numbers, references, masking, dates."""
import re
import secrets
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

NAIROBI = ZoneInfo("Africa/Nairobi")

# no 0/O or 1/I/L, so a code read aloud or off a screen is not mistyped
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def code(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def referral_code() -> str:
    return "ORBIS-" + code(5)


def reference(prefix: str) -> str:
    """Our own transaction reference, e.g. DP-7KQ2M9XH4T. Providers echo it back."""
    return f"{prefix}-{code(10)}"


# ------------------------------------------------------------------ money --
def usd(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def kes_up(amount_usd: Decimal, rate: float) -> int:
    """KSh asked for on a deposit: rounded up, so the dollars credited are covered."""
    return int((amount_usd * Decimal(str(rate))).to_integral_value(rounding=ROUND_CEILING))


def kes_down(amount_usd: Decimal, rate: float) -> int:
    """KSh paid out on a withdrawal: rounded down, never more than the dollars taken."""
    return int((amount_usd * Decimal(str(rate))).to_integral_value(rounding=ROUND_FLOOR))


# ------------------------------------------------------------------ phones --
def kenyan_msisdn(raw: str) -> str | None:
    """Any common way of writing a Kenyan mobile number, as 2547XXXXXXXX / 2541XXXXXXXX."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("254") and len(digits) == 12:
        local = digits[3:]
    elif digits.startswith("0") and len(digits) == 10:
        local = digits[1:]
    elif len(digits) == 9 and digits[0] in "71":
        local = digits
    else:
        return None
    if local[0] not in "71":
        return None
    return "254" + local


def local_phone(msisdn: str) -> str:
    """2547XXXXXXXX → 07XXXXXXXX, the form PayHero's examples use."""
    return "0" + msisdn[3:]


def e164(msisdn: str) -> str:
    return "+" + msisdn


def mask_phone(msisdn: str) -> str:
    return f"+{msisdn[:3]} {msisdn[3]}•• ••• {msisdn[-3:]}"


def mask_tail(value: str, keep: int = 4) -> str:
    v = re.sub(r"\s", "", value or "")
    return "••••" + v[-keep:]


TRON_ADDRESS = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")


def mask_wallet(addr: str) -> str:
    return addr[:4] + "••••" + addr[-4:]


# ------------------------------------------------------------------- dates --
def show_date(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(NAIROBI).strftime("%d %b %Y, %H:%M").lstrip("0")


def show_day(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(NAIROBI).strftime("%d %b %Y").lstrip("0")
