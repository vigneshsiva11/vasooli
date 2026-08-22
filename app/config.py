"""Application settings, loaded from environment variables / `.env`."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed view of the process environment.

    Values come from the real environment first, then `.env`. Secrets have no
    defaults on purpose — an empty string means "not configured yet", which the
    modules that need them are responsible for checking.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "Vasooli"
    environment: str = "development"

    # MongoDB
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "vasooli"

    # Google Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_timeout_seconds: float = 20.0
    # The health probe gets its own, much shorter budget: `GET /` must stay fast
    # even when Gemini is hanging, so it cannot inherit the diagnosis timeout.
    gemini_health_timeout_seconds: float = 4.0
    # How long a reachability observation is trusted before it is re-checked.
    gemini_health_ttl_seconds: float = 60.0

    # Razorpay (test mode)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
