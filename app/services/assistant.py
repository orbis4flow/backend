"""The support assistant behind Live chat: a language model on Groq.

Groq's API key is free (console.groq.com → API Keys) and speaks the OpenAI
chat format. The key stays here; the browser only ever talks to this API.
The model knows obisflow's rules from the prompt below, never sees an
account, and hands anything it cannot answer to the support team.
"""
import httpx

from ..config import get_settings


class AssistantError(Exception):
    pass


def _prompt() -> str:
    s = get_settings()
    return f"""You are Orbi, the support assistant in obisflow's Live chat. obisflow is a trading platform
for short-term contracts (Buy, which wins if the price rises, and Sell, which wins if it falls, on markets such as forex, indices and crypto), with a demo account
and a real-money account. All prices on obisflow come from obisflow's own simulated price feed: they are
not quotes from the real markets the symbols are named after, and will differ from them. Say so plainly if asked.

What you know:
- Demo account: ${s.demo_balance_usd:,.0f} of practice money, resettable from the account menu.
- Deposits: M-Pesa (an STK push to the phone, converted at about KSh {s.usd_kes_rate:g} to $1), card
  ({s.card_fee_pct:g}% fee), or USDT on TRON (send the exact amount shown; it credits once confirmed).
  Minimum deposit ${s.min_deposit_usd:,.2f}.
- Withdrawals: minimum ${s.min_withdraw_usd:,.2f} plus a ${s.withdraw_fee_usd:,.2f} fee. Paid only to a
  verified method in the user's own name, after a review by the team, usually the same day.
- Real-money trading and withdrawals need identity verification (Verification page: ID and a selfie).
- Security: two-factor by email or SMS, login alerts and device management are on the Security page.
- Trading limits, cooling-off and self-exclusion are under Preferences.
- Human support: {s.support_email}.

How you answer:
- Short, friendly, plain words. A few sentences; steps as a short list when it helps.
- You cannot see or change anyone's account, balance or payments. For a specific payment, ask for the
  reference (it starts with DP- or WD-) and say the team will check it, or point to {s.support_email}.
- Never give financial advice, predictions or signals, and never promise profits. Trading carries risk.
- Never ask for passwords, one-time codes, PINs or full card numbers.
- If you are not sure, say so and offer the support team rather than guessing."""


async def reply(history: list[dict]) -> str:
    """history: [{"role": "user"|"assistant", "content": str}, …], oldest first."""
    s = get_settings()
    if not s.groq_api_key:
        raise AssistantError("The assistant is not switched on yet.")
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                             headers={"Authorization": f"Bearer {s.groq_api_key}"},
                             json={"model": s.groq_model, "temperature": 0.3, "max_tokens": 500,
                                   "messages": [{"role": "system", "content": _prompt()}, *history]})
    except httpx.HTTPError as e:
        raise AssistantError("The assistant is not reachable right now.") from e
    if r.status_code == 429:
        raise AssistantError("The assistant is busy. Try again in a minute.")
    if r.status_code >= 400:
        raise AssistantError(f"The assistant could not answer (HTTP {r.status_code}).")
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError) as e:
        raise AssistantError("The assistant gave an empty answer.") from e
