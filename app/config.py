"""Settings, read once from the environment (or a local .env file)."""
import os
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------------------------------------------------------------- app ---
    env: str = "production"                       # "development" relaxes a few checks
    frontend_url: str = "https://obisflow.com"   # links in emails, referral links, returns
    public_api_url: str = ""                      # this API's own URL; Render sets RENDER_EXTERNAL_URL
    cors_origins: str = ""                        # extra origins, comma separated (FRONTEND_URL is always allowed)
    # the admin API exists only with ADMIN_TOKEN set (32+ characters); ADMIN_URL is the admin site, always allowed
    admin_token: str = ""
    admin_url: str = ""

    # -------------------------------------------------------- supabase ---
    supabase_url: str = "https://mbazpiignlrayzbwbxth.supabase.co"
    supabase_publishable_key: str = "sb_publishable_1xYs8_Ardi3je8wpkE3qLg_QhXKFXJm"
    supabase_jwt_secret: str = ""                 # only for projects still on the legacy HS256 secret
    database_url: str = ""                        # Postgres connection string, password included

    # ---------------------------------------------------------- money ---
    usd_kes_rate: float = 129.5                   # what M-Pesa amounts are converted at
    min_deposit_usd: float = 5.0
    deposit_expire_s: int = 120                   # an M-Pesa prompt deposit unfinished this long is deleted
    checkout_expire_s: int = 900                  # the same for a payment page (card, Kora checkout), filled in elsewhere
    max_deposit_usd: float = 10000.0
    min_withdraw_usd: float = 5.0
    withdraw_fee_usd: float = 1.0                 # charged on top of the amount withdrawn
    card_fee_pct: float = 1.5                     # kept from card deposits, as the UI shows
    demo_balance_usd: float = 10000.0

    # ---------------------------------------------------------- M-Pesa ---
    mpesa_provider: str = "payhero"               # "payhero": PayHero only; "kora": Kora push → Kora checkout → PayHero

    # ------------------------------------------------------------ kora ---
    kora_base_url: str = "https://api.korapay.com/merchant/api/v1"
    kora_secret_key: str = ""                     # sk_test_… or sk_live_…

    # --------------------------------------------------------- payhero ---
    payhero_base_url: str = "https://backend.payhero.co.ke/api/v2"
    payhero_api_username: str = ""
    payhero_api_password: str = ""
    payhero_channel_id: int = 0                   # the M-Pesa till/paybill channel for STK pushes
    payhero_callback_token: str = ""              # secret in the callback URL; Render generates it

    # -------------------------------------------------------- paystack ---
    paystack_secret_key: str = ""
    paystack_currency: str = "KES"                # the currency the Paystack account settles in

    # ------------------------------------------- live chat assistant (Groq) ---
    groq_api_key: str = ""                         # free key from console.groq.com
    groq_model: str = "llama-3.3-70b-versatile"

    # ----------------------------------------------------- email (Resend) ---
    resend_api_key: str = ""
    email_from: str = "obisflow <no-reply@obisflow.com>"   # a sender on a domain verified in Resend
    support_email: str = "support@obisflow.com"

    # ------------------------------------- SMS (Celcom, Africa's Talking) ---
    # Celcom is used when both are set (as on Nexas); SMS_PROVIDER forces one.
    # The AFRICASTALKING_* names Nexas uses work too.
    at_username: str = Field(default="", validation_alias=AliasChoices("at_username", "africastalking_username"))
    at_api_key: str = Field(default="", validation_alias=AliasChoices("at_api_key", "africastalking_api_key"))
    at_sender_id: str = Field(default="", validation_alias=AliasChoices("at_sender_id", "africastalking_sender_id"))
    celcom_api_key: str = ""
    celcom_partner_id: str = ""
    celcom_shortcode: str = ""                     # the sender ID Celcom registered; never MPESA
    celcom_base_url: str = "https://isms.celcomafrica.com/api/services"
    sms_provider: str = "auto"                     # auto (Celcom first), celcom or africastalking

    # ----------------------------------------------- VIP M-Pesa demo rail ---
    vip_merchant: str = "OBISFLOW"                 # who the handset's receipts say was paid
    reference_prefix: str = ""                     # start of every demo receipt code, up to 4 letters/digits (as Venti)
    sms_link: str = ""                             # optional link at the end of every demo wallet text
    sms_daily_limit_kes: float = 0                 # optional daily limit quoted in every demo wallet text

    # ----------------------------------------------- market data (free) ---
    finnhub_api_key: str = ""                      # free key from finnhub.io, for market news

    # ---------------------------------------------- USDT deposits (TRON) ---
    usdt_deposit_address: str = "TXqLJrvZc9ouyVPai66WR55dvDVetR83BH"
    trongrid_api_key: str = ""                     # optional, free from trongrid.io; raises the rate limit
    usdt_contract: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"   # Tether USD on TRON
    usdt_request_minutes: int = 120                # how long an exact-amount request stays open

    # ------------------------------------------------- referral earnings ---
    referral_spread_pct: float = 0.75              # obisflow's spread, as a share of real-money volume

    @field_validator("frontend_url", "public_api_url", "supabase_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def api_url(self) -> str:
        """Where providers call back: PUBLIC_API_URL, or the address Render gives the service."""
        return (self.public_api_url or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")

    @property
    def cors_list(self) -> list[str]:
        items = [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]
        for extra in (self.frontend_url, self.admin_url.rstrip("/")):
            if extra and extra not in items:
                items.append(extra)
        return items

    @property
    def is_dev(self) -> bool:
        return self.env.lower() in ("dev", "development", "local")

    @property
    def kora_ready(self) -> bool:
        return bool(self.kora_secret_key)

    @property
    def mpesa_uses_kora(self) -> bool:
        return self.mpesa_provider.strip().lower() != "payhero"

    @property
    def mpesa_ready(self) -> bool:
        return (self.mpesa_uses_kora and self.kora_ready) or self.payhero_ready

    @property
    def payhero_ready(self) -> bool:
        return bool(self.payhero_api_username and self.payhero_api_password and self.payhero_channel_id)

    @property
    def paystack_ready(self) -> bool:
        return bool(self.paystack_secret_key)

    @property
    def email_ready(self) -> bool:
        return bool(self.resend_api_key)

    @property
    def at_ready(self) -> bool:
        return bool(self.at_username and self.at_api_key)

    @property
    def celcom_ready(self) -> bool:
        return bool(self.celcom_api_key and self.celcom_partner_id and self.celcom_shortcode)

    @property
    def sms_gateway(self) -> str | None:
        """Which gateway texts go through: SMS_PROVIDER when that one is set up,
        else Celcom, else Africa's Talking; None when neither is."""
        want = self.sms_provider.strip().lower()
        if want == "africastalking" and self.at_ready:
            return "africastalking"
        if want == "celcom" and self.celcom_ready:
            return "celcom"
        if self.celcom_ready:
            return "celcom"
        return "africastalking" if self.at_ready else None

    @property
    def sms_ready(self) -> bool:
        return self.sms_gateway is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
