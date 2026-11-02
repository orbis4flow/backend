"""Sending a text: one door, two gateways behind it (as on Nexas).

Celcom Africa is used when both are configured (the registered sender ID),
Africa's Talking when only it is, and with neither nothing is sent. SMS_PROVIDER
forces one. Nothing falls back mid-send: a gateway that answered and refused
has given an answer, and re-sending through the other is how somebody gets the
same receipt twice.

send() never raises. It answers {ok, status, detail, provider, message_id},
where status is 'sent', 'off' (nothing configured), 'refused' (a gateway
answered no) or 'down' (it did not answer).

  Celcom:           https://celcomafrica.com (POST {base}/sendsms/)
  Africa's Talking: https://developers.africastalking.com/docs/sms/sending
"""
import httpx

from ..config import get_settings
from ..util import kenyan_msisdn

TIMEOUT = 12
LABEL = {"celcom": "Celcom", "africastalking": "Africa's Talking"}

# Africa's Talking: 100 processed, 101 sent, 102 queued all mean it has the message
AT_ACCEPTED = {100, 101, 102}
AT_REASONS = {401: "held for risk review", 402: "that sender ID is not registered", 403: "not a valid phone number",
              404: "that kind of number is not supported", 405: "not enough credit on the Africa's Talking account",
              406: "the number has blocked this sender", 407: "could not be routed to the network",
              409: "rejected by do-not-disturb", 500: "their server errored", 501: "their gateway errored",
              502: "rejected by the network"}


def provider() -> str | None:
    """The label of the gateway in use, for the admin site and the logs."""
    g = get_settings().sms_gateway
    return LABEL[g] if g else None


def sender() -> str | None:
    """What a message will say it came from."""
    s = get_settings()
    if s.sms_gateway == "celcom":
        return s.celcom_shortcode
    if s.sms_gateway == "africastalking":
        return (s.at_sender_id if s.at_username != "sandbox" else "") or "AFRICASTKNG"
    return None


def _out(ok: bool, status: str, detail: str, g: str | None, message_id=None) -> dict:
    return {"ok": ok, "status": status, "detail": detail, "provider": LABEL.get(g or ""), "message_id": message_id}


async def send(to: str, message: str) -> dict:
    s = get_settings()
    g = s.sms_gateway
    if not g:
        return _out(False, "off", "No SMS gateway is configured", None)
    raw = (to or "").strip()
    msisdn = kenyan_msisdn(raw)
    if not msisdn:                                   # a non-Kenyan number with its country code
        digits = "".join(ch for ch in raw if ch.isdigit())
        msisdn = digits if raw.startswith("+") and 8 <= len(digits) <= 15 else None
    if not msisdn:
        return _out(False, "refused", f"Not a phone number: {to}", g)
    message = (message or "")[:900]
    try:
        if g == "celcom":
            return await _celcom(msisdn, message)
        return await _africastalking(msisdn, message)
    except httpx.TimeoutException:
        return _out(False, "down", f"No answer within {TIMEOUT}s", g)
    except Exception as e:                           # never let a text fail the request behind it
        return _out(False, "down", str(e)[:200] or "Unreachable", g)


async def _celcom(msisdn: str, message: str) -> dict:
    s = get_settings()
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{s.celcom_base_url.rstrip('/')}/sendsms/",
                         json={"apikey": s.celcom_api_key, "partnerID": s.celcom_partner_id,
                               "shortcode": s.celcom_shortcode, "mobile": msisdn, "message": message},
                         headers={"Accept": "application/json"})
    try:
        body = r.json()
    except ValueError:
        return _out(False, "refused" if r.is_success else "down",
                    f"HTTP {r.status_code}: {r.text[:160] or 'empty reply'}", "celcom")
    entry = (body.get("responses") or [body])[0] if isinstance(body, dict) else {}
    # their code key is "response-code", and "respose-code" in some replies
    raw = entry.get("response-code", entry.get("respose-code", entry.get("code")))
    try:
        code = int(raw)
    except (TypeError, ValueError):
        code = 0
    detail = entry.get("response-description") or entry.get("respose-description") or f"code {code}"
    if code == 200:
        return _out(True, "sent", detail, "celcom", entry.get("messageid"))
    return _out(False, "refused", f"{detail} ({code or 'no code'})", "celcom")


async def _africastalking(msisdn: str, message: str) -> dict:
    s = get_settings()
    sandbox = s.at_username == "sandbox"
    base = "https://api.sandbox.africastalking.com" if sandbox else "https://api.africastalking.com"
    form = {"username": s.at_username, "to": "+" + msisdn, "message": message}
    # the sandbox has no sender IDs: given one, it answers 2xx with nobody in the
    # recipient list and the message vanishes, so it is left off there
    if s.at_sender_id and not sandbox:
        form["from"] = s.at_sender_id
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{base}/version1/messaging", data=form,
                         headers={"apiKey": s.at_api_key, "Accept": "application/json"})
    if r.status_code in (401, 403):
        return _out(False, "refused", f"the {'sandbox' if sandbox else 'live'} host refused the API key "
                                      f"(HTTP {r.status_code}); AT_USERNAME and AT_API_KEY must be from the same app",
                    "africastalking")
    try:
        body = r.json() if r.content else {}
    except ValueError:
        return _out(False, "refused" if r.is_success else "down",
                    f"HTTP {r.status_code}: {r.text[:160] or 'empty reply'}", "africastalking")
    data = body.get("SMSMessageData") or {}
    rec = (data.get("Recipients") or [None])[0]
    if not rec:                                      # nothing was even attempted
        why = (f'; the sender ID "{s.at_sender_id}" is probably not approved yet'
               if s.at_sender_id and not sandbox else "")
        return _out(False, "refused", (data.get("Message") or f"HTTP {r.status_code}: nothing accepted") + why,
                    "africastalking")
    code = int(rec.get("statusCode") or 0)
    if code in AT_ACCEPTED:
        return _out(True, "sent", (rec.get("status") or "Success") +
                    (" (sandbox: it reaches the simulator, not a phone)" if sandbox else ""),
                    "africastalking", rec.get("messageId"))
    return _out(False, "refused", f"{AT_REASONS.get(code) or rec.get('status') or 'refused'} ({code or 'no code'})",
                "africastalking")
