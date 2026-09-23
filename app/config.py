"""Settings, read once from the environment (or a local .env file)."""
import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------------------------------------------------------------- app ---
    env: str = "production"                       # "development" relaxes a few checks
    frontend_url: str = "https://orbisflow.com"   # links in emails, referral links, returns
    public_api_url: str = ""                      # this API's own URL; Render sets RENDER_EXTERNAL_URL
    cors_origins: str = ""                        # extra origins, comma separated (FRONTEND_URL is always allowed)

    # -------------------------------------------------------- supabase ---
    supabase_url: str = "https://mbazpiignlrayzbwbxth.supabase.co"
    supabase_publishable_key: str = "sb_publishable_1xYs8_Ardi3je8wpkE3qLg_QhXKFXJm"
    supabase_jwt_secret: str = ""                 # only for projects still on the legacy HS256 secret
    database_url: str = ""                        # Postgres connection string, password included

    # ---------------------------------------------------------- money ---
    usd_kes_rate: float = 129.5                   # what M-Pesa amounts are converted at
    min_deposit_usd: float = 2.0
    max_deposit_usd: float = 10000.0
    min_withdraw_usd: float = 5.0
    withdraw_fee_usd: float = 1.0                 # charged on top of the amount withdrawn
    card_fee_pct: float = 1.5                     # kept from card deposits, as the UI shows
    demo_balance_usd: float = 10000.0

    # --------------------------------------------------------- payhero ---
    payhero_base_url: str = "https://backend.payhero.co.ke/api/v2"
    payhero_api_username: str = ""
    payhero_api_password: str = ""
    payhero_channel_id: int = 0                   # the M-Pesa till/paybill channel for STK pushes
    payhero_callback_token: str = ""              # secret in the callback URL; Render generates it

    # -------------------------------------------------------- paystack ---
    paystack_secret_key: str = ""
    paystack_currency: str = "KES"                # the currency the Paystack account settles in

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
        if self.frontend_url and self.frontend_url not in items:
            items.append(self.frontend_url)
        return items

    @property
    def is_dev(self) -> bool:
        return self.env.lower() in ("dev", "development", "local")

    @property
    def payhero_ready(self) -> bool:
        return bool(self.payhero_api_username and self.payhero_api_password and self.payhero_channel_id)

    @property
    def paystack_ready(self) -> bool:
        return bool(self.paystack_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
