"""Email (Resend) and SMS (Celcom or Africa's Talking, services/sms.py).

Everything here is sent in the background: a slow or failing provider must
never slow down or fail the request that caused the message. Each attempt,
sent, failed or skipped for want of a key, is written to app.notifications.

  Resend:           https://resend.com/docs/api-reference/emails/send-email
"""
import asyncio
import html
import re
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
          button: tuple[str, str] | None = None, *, eyebrow: str | None = None, preheader: str | None = None,
          code: str | None = None, note: str | None = None, tone: str = "brand") -> str:
    """One branded email: a dark header with the mark, an eyebrow and a large
    title, the message, an optional highlighted code, a card of details, one
    green button and a quiet footer. Inline styles and tables only, because
    mail clients (Gmail, Outlook) drop everything else; the logo is a PNG
    since most clients block SVG."""
    s = get_settings()
    site = s.frontend_url
    accent = {"brand": "#00994F", "warn": "#B8860B", "bad": "#D93B39"}.get(tone, "#00994F")
    font = "Inter,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    mono = "'IBM Plex Mono',Consolas,Menlo,monospace"
    hidden = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">'
              f'{html.escape(preheader or title)}&#8199;&#847;&#8199;&#847;&#8199;&#847;</div>')
    head = (
        '<tr><td style="background:#141413;padding:26px 36px">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="vertical-align:middle;padding-right:12px"><img src="{html.escape(site)}/assets/img/email-mark.png" '
        'width="34" height="34" alt="" style="display:block;border:0"></td>'
        f'<td style="vertical-align:middle;font-family:Poppins,{font};font-size:21px;font-weight:700;letter-spacing:-0.3px;color:#ffffff">'
        'obis<span style="border-bottom:3px solid #00BF63;padding-bottom:1px">flow</span></td>'
        '</tr></table></td></tr>'
        f'<tr><td style="height:3px;background:{accent};font-size:0;line-height:0">&nbsp;</td></tr>')
    eye = (f'<p style="margin:0 0 10px;font-family:{font};font-size:11px;font-weight:600;letter-spacing:1.6px;'
           f'text-transform:uppercase;color:{accent}">{html.escape(eyebrow)}</p>') if eyebrow else ""
    h1 = (f'<h1 style="margin:0 0 18px;font-family:{font};font-size:26px;line-height:1.25;font-weight:700;'
          f'letter-spacing:-0.4px;color:#141413">{html.escape(title)}</h1>')
    body = "".join(f'<p style="margin:0 0 14px;font-family:{font};font-size:15px;line-height:1.65;color:#45443f">{l}</p>'
                   for l in lines)
    big = ""
    if code:
        big = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 22px">'
               f'<tr><td align="center" style="background:#f4f3ef;border:1px solid #e8e6e1;padding:20px 12px;'
               f'font-family:{mono};font-size:28px;font-weight:600;letter-spacing:6px;color:#141413">{html.escape(code)}</td></tr></table>')
    card = ""
    if rows:
        cells = "".join(
            f'<tr><td style="padding:12px 18px;{"border-top:1px solid #ecebe7;" if i else ""}font-family:{font};font-size:13px;color:#7a7973">'
            f'{html.escape(k)}</td><td align="right" style="padding:12px 18px;{"border-top:1px solid #ecebe7;" if i else ""}'
            f'font-family:{mono};font-size:13.5px;font-weight:500;color:#141413;word-break:break-all">{html.escape(v)}</td></tr>'
            for i, (k, v) in enumerate(rows))
        card = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="margin:8px 0 24px;background:#fafaf8;border:1px solid #e8e6e1;border-collapse:separate">{cells}</table>')
    btn = ""
    if button:
        btn = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:6px 0 8px"><tr>'
               f'<td style="background:#00994F"><a href="{html.escape(button[1])}" style="display:inline-block;padding:14px 26px;'
               f'font-family:{font};font-size:14.5px;font-weight:600;color:#ffffff;text-decoration:none">'
               f'{html.escape(button[0])} &rarr;</a></td></tr></table>')
    callout = ""
    if note:
        callout = (f'<p style="margin:22px 0 0;padding:14px 16px;background:#f7f6f2;border-left:3px solid {accent};'
                   f'font-family:{font};font-size:13px;line-height:1.6;color:#5e5e59">{note}</p>')
    foot = (
        f'<tr><td style="padding:26px 36px;background:#fafaf8;border-top:1px solid #ecebe7;font-family:{font};'
        'font-size:12px;line-height:1.7;color:#93928c">'
        f'Questions? Reply to this email or write to <a href="mailto:{html.escape(s.support_email)}" '
        f'style="color:#5e5e59">{html.escape(s.support_email)}</a>.<br>'
        f'<a href="{html.escape(site)}" style="color:#5e5e59;text-decoration:none">obisflow</a> &middot; Nairobi, Kenya<br>'
        '<span style="color:#b0afa9">Binary options carry a high risk of losing money rapidly. '
        'Only trade money you can afford to lose.</span></td></tr>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light"><title>' + html.escape(title) + '</title></head>'
        f'<body style="margin:0;padding:0;background:#efeee9">{hidden}'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#efeee9;padding:32px 12px">'
        '<tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;'
        'border:1px solid #e3e1db">'
        f'{head}<tr><td style="padding:36px 36px 32px">{eye}{h1}{body}{big}{card}{btn}{callout}</td></tr>{foot}'
        '</table></td></tr></table></body></html>')


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


def _text(title: str, lines: list[str], rows=None, button=None, code=None, note=None) -> str:
    strip = lambda t: re.sub(r"<[^>]+>", "", html.unescape(t))
    return "\n\n".join([title] + [strip(l) for l in lines] + ([code] if code else []) +
                         [f"{k}: {v}" for k, v in (rows or [])] + ([f"{button[0]}: {button[1]}"] if button else []) +
                         ([strip(note)] if note else []))


def email(user_id, kind: str, to: str, subject: str, title: str, lines: list[str],
          rows: list[tuple[str, str]] | None = None, button: tuple[str, str] | None = None, **style) -> None:
    """Queue one email. `style` passes eyebrow, preheader, code, note and tone to _page."""
    text = _text(title, lines, rows, button, style.get("code"), style.get("note"))
    _later(_send_email(user_id, kind, to, subject, _page(title, lines, rows, button, **style), text))


# --------------------------------------------------------------------- SMS --
async def _send_sms(user_id, kind, to, message) -> bool:
    """Through whichever gateway is set up (services/sms.py), recorded either way."""
    from . import sms as gateway
    if not to:
        await _record(user_id, "sms", kind, "-", None, "skipped", error="no phone number")
        return False
    out = await gateway.send(to, message)
    if out["status"] == "off":
        await _record(user_id, "sms", kind, to, None, "skipped", error="no SMS gateway configured")
        return False
    if out["ok"]:
        await _record(user_id, "sms", kind, to, None, "sent", provider_id=out.get("message_id"))
        return True
    log.warning("sms %s to %s failed (%s): %s", kind, to, out.get("provider"), out["detail"])
    await _record(user_id, "sms", kind, to, None, "failed", error=f"{out.get('provider')}: {out['detail']}"[:300])
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
          button=("Open trading", f"{s.frontend_url}/trade"), eyebrow="Account ready",
          note="Before your first withdrawal we ask for a photo of your ID. Sending it early from the "
               "Verification page means your money is never held up.")


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
          rows, ("View cashier", f"{s.frontend_url}/cashier"), eyebrow="Deposit received",
          preheader=f"{_money(tx['net_usd'])} is in your real account.")
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
           ("Taken from balance", _money(tx["net_usd"])), ("Reference", tx["reference"])],
          eyebrow="Withdrawal requested", preheader=f"{local}, usually paid the same day.")
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
          [f"{local} has been sent to {html.escape(tx.get('destination') or 'your account')}."], rows,
          eyebrow="Withdrawal paid", preheader=f"{local} is on its way to you.")
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
          [("Reference", tx["reference"])], ("View cashier", f"{s.frontend_url}/cashier"),
          eyebrow="Withdrawal not paid", tone="bad")


async def verification_decided(user_id, kind: str, doc_name: str, approved: bool, reason: str | None) -> None:
    p = await _person(user_id)
    if not p:
        return
    s = get_settings()
    what = "identity" if kind == "identity" else "address"
    if approved:
        email(p["id"], "verification_approved", p["email"], f"Your {what} is verified",
              f"Your {what} is verified",
              [f"Your {html.escape(doc_name)} was checked and approved."] +
              (["Withdrawals are open on your account."] if kind == "identity" else []),
              [("Document", doc_name), ("Status", "Approved")], ("Open cashier", f"{s.frontend_url}/cashier"),
              eyebrow="Verification approved")
        sms(p["id"], "verification_approved", p["phone"], f"obisflow: your {what} is verified."
            + (" Withdrawals are open." if kind == "identity" else ""))
    else:
        email(p["id"], "verification_rejected", p["email"], f"We could not verify your {what}",
              f"We could not verify your {what}",
              [f"Your {html.escape(doc_name)} was not accepted: {html.escape(reason or '')}",
               "Send a new one from the Verification page."],
              [("Document", doc_name), ("Status", "Not accepted")], ("Open Verification", f"{s.frontend_url}/verification"),
              eyebrow="Verification", tone="warn",
              note="Photos should be sharp, in colour, with all four corners of the document in view.")


async def vip_balance(tx: dict, balance) -> None:
    """A text to a VIP when the admin site credits or debits its real balance.
    It names obisflow as the source, never a payment provider."""
    p = await _person(tx["user_id"])
    if not p:
        return
    what = "credited to" if tx["type"] == "credit" else "debited from"
    sms(p["id"], f"vip_{tx['type']}", p["phone"],
        f"obisflow: {_money(tx['net_usd'])} {what} your VIP account. Balance {_money(balance)}. Ref {tx['reference']}.")


async def method_added(user_id, label: str, masked: str) -> None:
    p = await _person(user_id)
    if not p:
        return
    email(p["id"], "method_added", p["email"], "A payment method was added",
          "New payment method on your account",
          [f"<b>{html.escape(label)}</b> ({html.escape(masked)}) was just added to your account.",
           "If this was not you, change your password and contact support straight away."],
          eyebrow="Security", tone="warn")


async def referral_paid(user_id, amount, period: str) -> None:
    p = await _person(user_id)
    if not p:
        return
    s = get_settings()
    email(p["id"], "referral_paid", p["email"], f"Referral earnings paid: {_money(amount)}",
          "Your referral earnings are in", [f"{_money(amount)} for {html.escape(period)} is now in your real balance."],
          button=("See earnings", f"{s.frontend_url}/referral-earnings"), eyebrow="Referral earnings")
    sms(p["id"], "referral_paid", p["phone"], f"obisflow: referral earnings of {_money(amount)} paid to your balance.")



# ------------------------------------------------------- sent and awaited --
async def code_now(user_id, channel: str, to: str, code: str, purpose_text: str) -> bool:
    """A one-time code, sent while the caller waits: it has to know whether it
    went, because the person on the other end is waiting for it."""
    if channel == "sms":
        return await _send_sms(user_id, "otp", to, f"obisflow code: {code}. It {purpose_text} and expires "
                                                    "in 10 minutes. Never share it with anyone.")
    lines = [f"Use this code: it {html.escape(purpose_text)} and expires in 10 minutes."]
    text = f"Your obisflow code is {code}. It {purpose_text} and expires in 10 minutes."
    # the code stays out of the subject: subjects are kept in app.notifications
    return await _send_email(user_id, "otp", to, "Your obisflow code", _page(
        "Your one-time code", lines, code=code, eyebrow="Security", preheader="Your obisflow code, valid for 10 minutes.",
        note="obisflow will never ask you for this code. If you did not ask for it, change your password."), text)


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
          ("Review sessions", f"{s.frontend_url}/security"), eyebrow="Security", tone="warn")


async def password_changed(user_id) -> None:
    p = await _person(user_id)
    if not p:
        return
    email(p["id"], "password_changed", p["email"], "Your obisflow password was changed",
          "Password changed",
          ["The password on your account was just changed, and every other device was signed out.",
           "If you did not do this, reset your password straight away and contact support."],
          eyebrow="Security", tone="warn")


async def twofa_changed(user_id, on: bool, channel: str | None) -> None:
    p = await _person(user_id)
    if not p:
        return
    how = "a text message" if channel == "sms" else "email"
    email(p["id"], "twofa_changed", p["email"],
          "Two-factor sign-in turned " + ("on" if on else "off"),
          "Two-factor sign-in " + ("is on" if on else "is off"),
          [f"Signing in now also needs a code sent by {how}." if on else
           "Signing in no longer needs a code. If you did not turn this off, contact support at once."],
          eyebrow="Security", tone="brand" if on else "warn")


# --------------------------------------------------------- verification --
async def verification_received(user_id, kind: str, doc_name: str) -> None:
    p = await _person(user_id)
    if not p:
        return
    s = get_settings()
    what = "identity" if kind == "identity" else "address"
    email(p["id"], "verification_received", p["email"], f"We have your proof of {what}",
          "Your document is with our team",
          [f"Thank you. Your <b>{html.escape(doc_name)}</b> arrived safely and is waiting for review.",
           "We usually finish within a working day, and we will email you the moment it is done."
           + (" Withdrawals open as soon as your identity is approved." if kind == "identity" else "")],
          [("Document", doc_name), ("Proves", what.capitalize()), ("Status", "In review")],
          ("See verification", f"{s.frontend_url}/verification"), eyebrow="Verification received",
          preheader="Usually reviewed within a working day.",
          note="Your documents are kept encrypted and seen only by our verification team.")


# ---------------------------------------------------------- admin staff --
_ROLE_TEXT = {"superadmin": ("Super admin", "everything on the admin site, including adding other admins"),
              "admin": ("Admin", "everything on the admin site except managing admins"),
              "finance": ("Finance", "paying and rejecting withdrawals, with read access to the rest"),
              "support": ("Support", "verifying IDs and payment methods, with read access to the rest"),
              "marketer": ("Marketer", "going live and seeing the money deposited during your own sessions")}


def _admin_link() -> str:
    s = get_settings()
    return (s.admin_url or "").rstrip("/") or s.frontend_url


async def admin_invite(to: str, name: str, role: str, password: str, by: str) -> bool:
    """A new admin's sign-in details, sent while the super admin waits so the
    admin site can say whether it went."""
    label, scope = _ROLE_TEXT.get(role, (role, "the admin site"))
    first = (name or "").split(" ")[0] or "there"
    lines = [f"Hi {html.escape(first)}, {html.escape(by)} has given you a <b>{label}</b> account on the obisflow admin site.",
             f"It covers {scope}. Sign in with the details below."]
    rows = [("Sign in at", _admin_link()), ("Email", to), ("Role", label)]
    note = ("This is a temporary password. Change it on your Account page after you sign in, and never share it. "
            "If you were not expecting this, ignore this email.")
    text = _text("Your obisflow admin account", lines, rows, None, password, note)
    return await _send_email(None, "admin_invite", to, "Your obisflow admin account", _page(
        "Your admin account is ready", lines, rows, ("Sign in", _admin_link()), eyebrow="obisflow admin",
        preheader=f"Your {label} sign-in for the obisflow admin site.", code=password, note=note), text)


async def admin_password_reset(to: str, name: str, password: str, by: str) -> bool:
    first = (name or "").split(" ")[0] or "there"
    lines = [f"Hi {html.escape(first)}, {html.escape(by)} has set a new password on your obisflow admin account.",
             "You were signed out everywhere. Sign in again with this password:"]
    note = "Change it on your Account page after you sign in. If you did not expect this, tell a super admin."
    text = _text("New admin password", lines, None, None, password, note)
    return await _send_email(None, "admin_password", to, "Your obisflow admin password was reset", _page(
        "Your password was reset", lines, [("Sign in at", _admin_link()), ("Email", to)], ("Sign in", _admin_link()),
        eyebrow="obisflow admin", code=password, note=note, tone="warn"), text)
