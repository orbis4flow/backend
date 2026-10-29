"""Email (Resend) and SMS (Africa's Talking).

Everything here is sent in the background: a slow or failing provider must
never slow down or fail the request that caused the message. Each attempt,
sent, failed or skipped for want of a key, is written to app.notifications.

  Resend:           https://resend.com/docs/api-reference/emails/send-email
  Africa's Talking: https://developers.africastalking.com/docs/sms/sending
"""
import asyncio
import html
import logging

import httpx

from .. import db
from ..config import get_settings

log = logging.getLogger("obisflow.notify")
_tasks: set[asyncio.Task] = set()


def _later(coro) -> None:
    """Run a send without waiting for it, and keep a reference so it is not
    collected half way through."""
    try:
        t = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _record(user_id, channel, kind, to, subject, status, provider_id=None, error=None) -> None:
    try:
        await db.run(
            """insert into app.notifications (user_id, channel, kind, to_address, subject, status, provider_id, error)
               values (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, channel, kind, to, subject, status, provider_id, (error or "")[:300] or None))
    except Exception:                       # sql/004 not run yet: sending still works
        log.warning("could not record %s %s to %s", channel, kind, to)


# ------------------------------------------------------------------- email --
def _page(title: str, lines: list[str], rows: list[tuple[str, str]] | None = None,
          button: tuple[str, str] | None = None) -> str:
    """One plain, branded email: a heading, a few lines, an optional table of
    figures and one button. Inline styles, because mail clients drop the rest."""
    s = get_settings()
    body = "".join(f'<p style="margin:0 0 12px;font-size:15px;line-height:1.6;color:#3a3a37">{l}</p>' for l in lines)
    table = ""
    if rows:
        table = '<table role="presentation" width="100%" style="border-collapse:collapse;margin:8px 0 18px">' + "".join(
            f'<tr><td style="padding:9px 0;border-top:1px solid #ecebe7;font-size:14px;color:#7a7973">{html.escape(k)}</td>'
            f'<td style="padding:9px 0;border-top:1px solid #ecebe7;font-size:14px;color:#1c1c1c;text-align:right;'
            f'font-family:Consolas,Menlo,monospace">{html.escape(v)}</td></tr>' for k, v in rows) + "</table>"
    btn = ""
    if button:
        btn = (f'<a href="{html.escape(button[1])}" style="display:inline-block;background:#00994F;color:#ffffff;'
               f'text-decoration:none;font-weight:600;font-size:14px;padding:12px 20px">{html.escape(button[0])}</a>')
    return (
        '<!doctype html><html><body style="margin:0;background:#fafaf9;font-family:Inter,Segoe UI,Arial,sans-serif">'
        '<table role="presentation" width="100%" style="background:#fafaf9;padding:28px 12px"><tr><td align="center">'
        '<table role="presentation" width="100%" style="max-width:520px;background:#ffffff;border:1px solid #e8e6e1">'
        '<tr><td style="padding:22px 26px;border-bottom:1px solid #ecebe7;font-size:18px;font-weight:700;color:#1c1c1c">'
        'obis<span style="border-bottom:2px solid #00BF63">flow</span></td></tr>'
        f'<tr><td style="padding:26px"><h1 style="margin:0 0 14px;font-size:20px;color:#1c1c1c">{html.escape(title)}</h1>'
        f'{body}{table}{btn}</td></tr>'
        '<tr><td style="padding:18px 26px;border-top:1px solid #ecebe7;font-size:12px;line-height:1.6;color:#93928c">'
        f'Questions? Reply to this email or write to {html.escape(s.support_email)}.<br>'
        'Binary options carry a high risk of losing money rapidly. Only trade money you can afford to lose.'
        '</td></tr></table></td></tr></table></body></html>')


async def _send_email(user_id, kind, to, subject, html_body, text) -> bool:
    s = get_settings()
    if not s.email_ready or not to:
        await _record(user_id, "email", kind, to or "-", subject, "skipped", error="RESEND_API_KEY not set")
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.resend.com/emails",
                             headers={"Authorization": f"Bearer {s.resend_api_key}"},
                             json={"from": s.email_from, "to": [to], "subject": subject, "html": html_body,
                                   "text": text, "reply_to": s.support_email})
        if r.status_code >= 400:
            raise RuntimeError(f"Resend {r.status_code}: {r.text[:200]}")
        await _record(user_id, "email", kind, to, subject, "sent", provider_id=(r.json() or {}).get("id"))
        return True
    except Exception as e:
        log.warning("email %s to %s failed: %s", kind, to, e)
        await _record(user_id, "email", kind, to, subject, "failed", error=str(e))
        return False


def email(user_id, kind: str, to: str, subject: str, title: str, lines: list[str],
          rows: list[tuple[str, str]] | None = None, button: tuple[str, str] | None = None) -> None:
    text = "\n\n".join([title] + [html.unescape(l) for l in lines] +
                       [f"{k}: {v}" for k, v in (rows or [])] + ([f"{button[0]}: {button[1]}"] if button else []))
    _later(_send_email(user_id, kind, to, subject, _page(title, lines, rows, button), text))


# --------------------------------------------------------------------- SMS --
# 100 processed, 101 sent, 102 queued: all three mean Africa's Talking has it
AT_ACCEPTED = {100, 101, 102}
AT_REASONS = {401: "held for risk review", 402: "that sender ID is not registered", 403: "not a valid phone number",
              404: "that kind of number is not supported", 405: "not enough credit on the Africa's Talking account",
              406: "the number has blocked this sender", 407: "could not be routed to the network",
              409: "rejected by do-not-disturb", 500: "their server errored", 501: "their gateway errored",
              502: "rejected by the network"}


async def _send_sms(user_id, kind, to, message) -> bool:
    s = get_settings()
    if not s.sms_ready or not to:
        await _record(user_id, "sms", kind, to or "-", None, "skipped", error="Africa's Talking not configured")
        return False
    sandbox = s.at_username == "sandbox"
    base = "https://api.sandbox.africastalking.com" if sandbox else "https://api.africastalking.com"
    form = {"username": s.at_username, "to": to, "message": message}
    # the sandbox has no sender IDs: it answers 2xx with nobody in the recipient list
    # when given one, and the message simply vanishes, so it is left off there
    if s.at_sender_id and not sandbox:
        form["from"] = s.at_sender_id
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/version1/messaging", data=form,
                             headers={"apiKey": s.at_api_key, "Accept": "application/json"})
        if r.status_code in (401, 403):
            raise RuntimeError(f"the {'sandbox' if sandbox else 'live'} host refused the API key (HTTP {r.status_code}); "
                               "AT_USERNAME and AT_API_KEY must be from the same app")
        body = r.json() if r.content else {}
        data = body.get("SMSMessageData") or {}
        rec = (data.get("Recipients") or [None])[0]
        if not rec:                                  # nothing was even attempted
            why = f'; the sender ID "{s.at_sender_id}" is probably not approved yet' if s.at_sender_id and not sandbox else ""
            raise RuntimeError((data.get("Message") or f"HTTP {r.status_code}: nothing accepted") + why)
        code = int(rec.get("statusCode") or 0)
        if code not in AT_ACCEPTED:
            raise RuntimeError(f"{AT_REASONS.get(code) or rec.get('status') or 'refused'} ({code or 'no code'})")
        await _record(user_id, "sms", kind, to, None, "sent", provider_id=rec.get("messageId"))
        return True
    except Exception as e:
        log.warning("sms %s to %s failed: %s", kind, to, e)
        await _record(user_id, "sms", kind, to, None, "failed", error=str(e)[:300])
        return False


def sms(user_id, kind: str, to: str | None, message: str) -> None:
    if to:
        _later(_send_sms(user_id, kind, to, message[:459]))     # three SMS parts at most


# ------------------------------------------------------ the messages sent --
def _money(v) -> str:
    return f"${float(v):,.2f}"


async def _person(user_id) -> dict | None:
    return await db.one("select id, email, full_name, phone from app.profiles where id = %s", (user_id,))


async def welcome(user_id) -> None:
    p = await _person(user_id)
    if not p:
        return
    s = get_settings()
    first = (p["full_name"] or "").split(" ")[0] or "there"
    email(p["id"], "welcome", p["email"], "Welcome to obisflow",
          f"Welcome, {html.escape(first)}",
          ["Your account is ready, with <b>$10,000</b> in demo funds to practise on before you risk anything real.",
           "When you are ready for real money, deposit by M-Pesa, card or USDT from the account menu."],
          button=("Open trading", f"{s.frontend_url}/trade"))


async def deposit_received(tx: dict) -> None:
    p = await _person(tx["user_id"])
    if not p:
        return
    s = get_settings()
    method = {"mpesa": "M-Pesa", "card": "card", "usdt": "USDT"}.get(tx["method"], tx["method"])
    rows = [("Credited", _money(tx["net_usd"])), ("Method", method), ("Reference", tx["reference"])]
    if tx.get("provider_receipt"):
        rows.append(("Receipt", tx["provider_receipt"]))
    email(p["id"], "deposit_received", p["email"], f"Deposit received: {_money(tx['net_usd'])}",
          "Your deposit has arrived", [f"{_money(tx['net_usd'])} is in your real account and ready to trade."],
          rows, ("View cashier", f"{s.frontend_url}/cashier"))
    sms(p["id"], "deposit_received", p["phone"],
        f"obisflow: deposit of {_money(tx['net_usd'])} received via {method}. Ref {tx['reference']}.")


async def withdrawal_requested(tx: dict) -> None:
    p = await _person(tx["user_id"])
    if not p:
        return
    local = f"KSh {int(tx['local_amount']):,}" if tx.get("local_amount") else _money(tx["amount_usd"])
    email(p["id"], "withdrawal_requested", p["email"], f"Withdrawal requested: {_money(tx['amount_usd'])}",
          "We have your withdrawal request",
          [f"{local} to {html.escape(tx.get('destination') or 'your account')}. Our team pays withdrawals "
           "usually the same day, and always within a working day.",
           "If you did not ask for this, reply to this email at once."],
          [("Amount", _money(tx["amount_usd"])), ("Fee", _money(tx["fee_usd"])),
           ("Taken from balance", _money(tx["net_usd"])), ("Reference", tx["reference"])])
    sms(p["id"], "withdrawal_requested", p["phone"],
        f"obisflow: withdrawal of {local} requested. Ref {tx['reference']}. Not you? Contact support now.")


async def withdrawal_paid(tx: dict) -> None:
    p = await _person(tx["user_id"])
    if not p:
        return
    local = f"KSh {int(tx['local_amount']):,}" if tx.get("local_amount") else _money(tx["amount_usd"])
    rows = [("Amount", local), ("Reference", tx["reference"])]
    if tx.get("provider_receipt"):
        rows.append(("Receipt", tx["provider_receipt"]))
    email(p["id"], "withdrawal_paid", p["email"], f"Withdrawal paid: {_money(tx['amount_usd'])}",
          "Your withdrawal has been paid",
          [f"{local} has been sent to {html.escape(tx.get('destination') or 'your account')}."], rows)
    sms(p["id"], "withdrawal_paid", p["phone"],
        f"obisflow: withdrawal of {local} paid. Ref {tx['reference']}.")


async def withdrawal_rejected(tx: dict) -> None:
    p = await _person(tx["user_id"])
    if not p:
        return
    s = get_settings()
    email(p["id"], "withdrawal_rejected", p["email"], "Your withdrawal was not paid",
          "Your withdrawal was not paid",
          [f"We could not pay your withdrawal of {_money(tx['amount_usd'])}: {html.escape(tx.get('failure_reason') or '')}",
           f"The full {_money(tx['net_usd'])} is back in your real account. Reply to this email if you have questions."],
          [("Reference", tx["reference"])], ("View cashier", f"{s.frontend_url}/cashier"))


async def method_added(user_id, label: str, masked: str) -> None:
    p = await _person(user_id)
    if not p:
        return
    email(p["id"], "method_added", p["email"], "A payment method was added",
          "New payment method on your account",
          [f"<b>{html.escape(label)}</b> ({html.escape(masked)}) was just added to your account.",
           "If this was not you, change your password and contact support straight away."])


async def referral_paid(user_id, amount, period: str) -> None:
    p = await _person(user_id)
    if not p:
        return
    s = get_settings()
    email(p["id"], "referral_paid", p["email"], f"Referral earnings paid: {_money(amount)}",
          "Your referral earnings are in", [f"{_money(amount)} for {html.escape(period)} is now in your real balance."],
          button=("See earnings", f"{s.frontend_url}/referral-earnings"))
    sms(p["id"], "referral_paid", p["phone"], f"obisflow: referral earnings of {_money(amount)} paid to your balance.")



# ------------------------------------------------------- sent and awaited --
async def code_now(user_id, channel: str, to: str, code: str, purpose_text: str) -> bool:
    """A one-time code, sent while the caller waits: it has to know whether it
    went, because the person on the other end is waiting for it."""
    if channel == "sms":
        return await _send_sms(user_id, "otp", to, f"obisflow code: {code}. It {purpose_text} and expires "
                                                    "in 10 minutes. Never share it with anyone.")
    lines = [f"Your code is <b style=\"font-size:22px;letter-spacing:4px;font-family:Consolas,monospace\">{code}</b>",
             f"It {html.escape(purpose_text)} and expires in 10 minutes.",
             "obisflow will never ask you for this code. If you did not ask for it, change your password."]
    text = f"Your obisflow code is {code}. It {purpose_text} and expires in 10 minutes."
    # the code stays out of the subject: subjects are kept in app.notifications
    return await _send_email(user_id, "otp", to, "Your obisflow code", _page("Your code", lines), text)


async def login_alert(user_id, device: str, ip: str | None) -> None:
    p = await _person(user_id)
    if not p:
        return
    s = get_settings()
    email(p["id"], "login_alert", p["email"], "New sign-in to your obisflow account",
          "A new device signed in",
          [f"Your account was just opened on <b>{html.escape(device or 'a new device')}</b>.",
           "If this was you, there is nothing to do. If not, sign that session out and change your password now."],
          [("Device", device or "Unknown"), ("IP address", ip or "Unknown")],
          ("Review sessions", f"{s.frontend_url}/security"))


async def password_changed(user_id) -> None:
    p = await _person(user_id)
    if not p:
        return
    email(p["id"], "password_changed", p["email"], "Your obisflow password was changed",
          "Password changed",
          ["The password on your account was just changed, and every other device was signed out.",
           "If you did not do this, reset your password straight away and contact support."])


async def twofa_changed(user_id, on: bool, channel: str | None) -> None:
    p = await _person(user_id)
    if not p:
        return
    how = "a text message" if channel == "sms" else "email"
    email(p["id"], "twofa_changed", p["email"],
          "Two-factor sign-in turned " + ("on" if on else "off"),
          "Two-factor sign-in " + ("is on" if on else "is off"),
          [f"Signing in now also needs a code sent by {how}." if on else
           "Signing in no longer needs a code. If you did not turn this off, contact support at once."])
